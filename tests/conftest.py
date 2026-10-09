import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from promobot.config import Settings
from promobot.crm import AlfaCRM
from promobot.dialog import Dialog
from promobot.storage import Store, dumps
from promobot.mock import MockCRM
from promobot.worker import Worker


@pytest.fixture
async def app(tmp_path):
    cfg = json.loads(Path("config/mock.json").read_text())
    s = Settings("test", "mock", tmp_path / "bot.sqlite3", cfg["branch"],
                 {10002: "preobrazhenka", 10003: "preobrazhenka", 10004: "preobrazhenka"}, 10001,
                 retry_base=0)
    db = await Store(s.database).open()
    await db.bind(s.environment, "42", s.branch)
    mock = await MockCRM(tmp_path / "crm.sqlite3", s).open()
    crm = AlfaCRM(s, mock.client())
    crm.limiter.interval = 0
    dialog = Dialog(db, s, "42")
    worker = Worker(db, s, crm)
    counter = 0
    async def event(text=None, action=None, uid=10002, update_id=None, chat_type="private", callback=None, date=1790992800, username=None):
        nonlocal counter
        counter += 1
        n = update_id if update_id is not None else counter
        msg = {"message_id": n, "date": date, "from": {"id": uid, "is_bot": False, "first_name": "P"},
               "chat": {"id": uid, "type": chat_type}, "text": text} if text is not None else {
               "message_id": n, "date": date, "from": {"id": uid, "is_bot": False, "first_name": "P"},
               "chat": {"id": uid, "type": chat_type}}
        if username:
            msg["from"]["username"] = username
        if action:
            ds = await db.query("SELECT * FROM drafts WHERE user_id=? AND active=1", (uid,))
            if action.startswith("menu:"):
                callback = action
            else:
                d = ds[0]
                callback = f'd:{d["id"]}:{d["version"]}:{action}'
        if callback:
            u = {"update_id": n, "callback_query": {"id": f"q{n}", "from": msg["from"], "message": msg, "data": callback}}
        else:
            u = {"update_id": n, "message": msg}
        await db.ingest("42", [u])
        await dialog.process(n)
        return u
    async def draft(uid=10002):
        ds = await db.query("SELECT * FROM drafts WHERE user_id=? AND active=1", (uid,))
        if not ds:
            return None
        d = ds[0]
        d["data"] = json.loads(d["data"])
        return d
    async def legacy_new(uid=10002):
        await event(action='menu:new', uid=uid)
        await db.execute("UPDATE drafts SET step='parent',paused=0 WHERE user_id=? AND active=1", (uid,))
    async def intake(uid=10002, parent="Анна", kids=(("Алиса", "9"),), telephone="8 (999) 123-45-67", telegram=None, comment="Алису очень заинтересовала робототехника", confirm=True, branch=None):
        if branch:
            await event(callback='branch:select:' + branch, uid=uid)
        await event(action="menu:new", uid=uid)
        # Exercise compatibility with an existing pre-0.4 wizard draft. New
        # single-message submissions have dedicated integration coverage.
        await db.execute("UPDATE drafts SET step='parent',paused=0 WHERE user_id=? AND active=1", (uid,))
        await event(parent, uid=uid)
        await event(action="phone" if telephone else "username", uid=uid)
        await event(telephone or telegram, uid=uid)
        if telephone and telegram:
            await event(action="username", uid=uid)
            await event(telegram, uid=uid)
        await event(action="next", uid=uid)
        if telephone:
            await event(action="msg0", uid=uid)
            await event(action="msg1", uid=uid)
            await event(action="next", uid=uid)
        await event(action="pref0", uid=uid)
        for i, (kid, years) in enumerate(kids):
            if i:
                await event(action="addchild", uid=uid)
            await event(kid, uid=uid)
            await event(years, uid=uid)
        await event(action="next", uid=uid)
        if comment:
            await event(action="comment", uid=uid)
            await event(comment, uid=uid)
        else:
            await event(action="skip", uid=uid)
        if confirm and (await draft(uid))["step"] == "review":
            await event(action="confirm", uid=uid)
        result = await db.query("SELECT * FROM requests WHERE user_id=? ORDER BY saved_at DESC", (uid,))
        return result[0] if result else None
    yield SimpleNamespace(db=db, s=s, crm=crm, mock=mock, dialog=dialog, worker=worker,
                          event=event, draft=draft, intake=intake, legacy_new=legacy_new)
    await crm.close()
    await mock.close()
    await db.close()
