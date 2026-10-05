import asyncio
from types import SimpleNamespace
import pytest
from promobot.storage import enqueue
from promobot.telegram import Outbox,Runtime


async def test_slow_chat_does_not_block_another_chat_or_reorder_its_own_messages(app):
    started,release=asyncio.Event(),asyncio.Event()
    sent=[]
    class Bot:
        async def send_message(self,chat,text,**kwargs):
            if text=='first':
                started.set();await release.wait()
            sent.append((chat,text))
            return SimpleNamespace(message_id=len(sent))
    async with app.db.tx() as c:
        await enqueue(c,'a1',20001,'first')
        await enqueue(c,'a2',20001,'second')
        await enqueue(c,'b1',20002,'independent')
    out=Outbox(app.db,Bot(),app.s)
    slow=asyncio.create_task(out.tick())
    await asyncio.wait_for(started.wait(),2)
    try:
        assert await asyncio.wait_for(out.tick(),2)
        assert sent==[(20002,'independent')]
        assert not await out.tick()
        release.set();await slow
        assert await out.tick()
        assert sent==[(20002,'independent'),(20001,'first'),(20001,'second')]
        assert len(await app.db.query("SELECT id FROM outbox WHERE state='done'"))==3
    finally:
        release.set();await slow


@pytest.mark.parametrize('signal_inside_tick',[False,True])
async def test_queue_wakeup_does_not_wait_for_idle_timeout_or_lose_a_signal(app,signal_inside_tick):
    runtime=Runtime(app.db,app.s,None,app.dialog,app.worker)
    waiting,processed=asyncio.Event(),asyncio.Event()
    calls=0
    async def tick():
        nonlocal calls
        calls+=1
        if calls==1:
            waiting.set()
            if signal_inside_tick:runtime.jobs_ready.set()
        else:processed.set()
        return False
    task=asyncio.create_task(runtime.loop(tick,60,runtime.jobs_ready))
    try:
        await waiting.wait()
        if not signal_inside_tick:runtime.jobs_ready.set()
        await asyncio.wait_for(processed.wait(),2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task


async def test_crm_scan_is_shared_only_within_attempt_and_refreshed_for_next_lead(app):
    scans=0
    original=app.crm.customers
    async def customers():
        nonlocal scans
        scans+=1
        return await original()
    app.crm.customers=customers
    await app.intake()
    await app.worker.tick()
    assert scans==1
    assert (await app.db.query('SELECT state FROM requests'))[0]['state']=='delivered'
    await app.intake(uid=10003,telephone='+79991234568')
    existing=await app.crm.create({'name':'Added between attempts','branch_ids':[901],'phone':['+79991234568'],'web':[]})
    await app.worker.tick()
    assert scans==2
    rows=await app.db.query('SELECT state,matches FROM requests ORDER BY saved_at')
    assert rows[1]['state']=='duplicate_review'
    assert str(existing['id']) in rows[1]['matches']
