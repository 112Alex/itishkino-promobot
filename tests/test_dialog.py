import asyncio
import json
import pytest
from promobot.dialog import Dialog
from promobot.domain import InputError, age, name, phone, username, moscow
from promobot.storage import Store


async def test_one_child_delivery_and_comment(app):
    r = await app.intake()
    await app.worker.tick()
    row = (await app.db.query("SELECT * FROM requests"))[0]
    assert row["state"] == "delivered"
    models = await app.crm.customers()
    assert len(models) == 1 and models[0]["name"] == "Анна Алиса 9"
    assert models[0]["note"].endswith("\nписать")
    assert "Алису очень заинтересовала робототехника" not in models[0]["note"]
    comments = await app.crm.comments(row["crm_id"])
    assert len(comments) == 1 and comments[0]["user_id"] == 990
    assert "Алису очень заинтересовала робототехника" in comments[0]["comment"]
    assert r["id"] not in comments[0]["comment"]
    assert "Мессенджеры: MAX, Telegram" in comments[0]["comment"]
    assert models[0]["teacher_ids"] == [] and models[0]["assigned_id"] is None
    assert "dob" not in models[0]


async def test_two_kids_one_lead(app):
    await app.intake(parent="Настя", kids=(("Мария", "7"), ("Денис", "8")))
    await app.worker.tick()
    models = await app.crm.customers()
    assert len(models) == 1 and models[0]["name"] == "Настя Мария 7 Денис 8"
    comment = (await app.crm.comments(models[0]["id"]))[0]["comment"]
    assert "1. Мария — 7" in comment and "2. Денис — 8" in comment
    assert "Мария" not in models[0]["note"]


async def test_username_only_and_no_empty_comment(app):
    await app.intake(telephone=None, telegram="https://t.me/My_Parent", comment=None)
    await app.worker.tick()
    lead = (await app.crm.customers())[0]
    assert lead["phone"] == []
    assert lead["web"] == ["https://t.me/my_parent"]
    comments = await app.crm.comments(lead["id"])
    assert len(comments) == 1 and "Родитель: Анна" in comments[0]["comment"]
    assert "None" not in comments[0]["comment"]
    assert lead["note"].endswith("\nписать")


@pytest.mark.parametrize("bad", ["abc", "123", "+999999999999"])
async def test_bad_phone_keeps_step(app, bad):
    await app.legacy_new()
    await app.event("Анна")
    await app.event(action="phone")
    before = await app.draft()
    await app.event(bad)
    after = await app.draft()
    assert after["step"] == "phone" and after["data"] == before["data"]


async def test_bad_age_unsupported_and_unknown(app):
    await app.intake(confirm=False)
    await app.event(action="edit")
    await app.event(action="eage0")
    before = await app.draft()
    for bad in ("1.5", "90", "не знаю", None):
        await app.event(bad)
        assert (await app.draft())["data"] == before["data"]
    await app.event(action="unknown")
    assert (await app.draft())["data"]["children"][0]["age"] is None


async def test_long_title_explicit_shortening_preserves_data(app):
    parent = "Анна <родитель>"
    child = "Очень длинное имя ребёнка с несколькими словами"
    await app.intake(parent=parent, kids=((child, "9"),), comment="Нельзя <сладкое>\nПросили после 19:00", confirm=False)
    d = await app.draft()
    assert d["step"] == "title"
    await app.event("Анна семья")
    await app.event(action="confirm")
    await app.worker.tick()
    model = (await app.crm.customers())[0]
    assert model["name"] == "Анна семья"
    comment = (await app.crm.comments(model["id"]))[0]["comment"]
    assert child in comment
    assert "&lt;сладкое&gt;\nПросили после 19:00" in comment
    assert child not in model["note"]


async def test_three_promoters_isolated(app):
    await asyncio.gather(*(app.intake(uid=u, telephone=f"+7999123456{i}") for i, u in enumerate((10002, 10003, 10004))))
    requests = await app.db.query("SELECT * FROM requests")
    assert len(requests) == 3 and len({r["user_id"] for r in requests}) == 3
    assert len({json.loads(r["data"])["phones"][0] for r in requests}) == 3


async def test_slow_crm_does_not_block_intake(app):
    await app.intake()
    started, release = asyncio.Event(), asyncio.Event()
    original = app.crm.own
    async def slow(ident, records=None):
        started.set()
        await release.wait()
        return await original(ident, records)
    app.crm.own = slow
    task = asyncio.create_task(app.worker.tick())
    await started.wait()
    await asyncio.wait_for(app.intake(uid=10003, telephone="+79991234568"), 2)
    release.set()
    await task
    assert len(await app.db.query("SELECT * FROM requests")) == 2


async def test_duplicate_update_and_callback(app):
    await app.intake(confirm=False)
    d = await app.draft()
    callback = f'd:{d["id"]}:{d["version"]}:confirm'
    event = await app.event(callback=callback)
    await app.db.ingest("42", [event])
    await app.dialog.process(event["update_id"])
    await app.event(callback=callback)
    assert len(await app.db.query("SELECT * FROM requests")) == 1
    assert len(await app.db.query("SELECT * FROM jobs")) == 1


async def test_foreign_and_old_buttons(app):
    await app.legacy_new()
    d = await app.draft()
    old = f'd:{d["id"]}:{d["version"]}:cancel'
    await app.event("Анна")
    await app.event(callback=old)
    assert (await app.draft())["step"] == "contact_type"
    await app.event(callback=f'd:{d["id"]}:{d["version"]+1}:cancel', uid=10003)
    assert await app.draft()


async def test_order_two_fast_replies(app):
    await app.legacy_new()
    await app.event("Анна")
    await app.event(action="username")
    await app.event("@my_parent")
    await app.event(action="next")
    await app.event(action="pref0")
    events = []
    for ident, text in ((800, "Алиса"), (801, "9")):
        events.append({"update_id": ident, "message": {"message_id": ident, "date": 1790992800,
                       "from": {"id": 10002}, "chat": {"id": 10002, "type": "private"}, "text": text}})
    await app.db.ingest("42", events)
    for event in events:
        await app.dialog.process(event["update_id"])
    await app.dialog.process(801)
    assert (await app.draft())["data"]["children"] == [{"name": "Алиса", "age": 9}]


async def test_restart_stale_draft_and_inbox_recovery(app):
    u = {"update_id": 500, "message": {"message_id": 500, "date": 1790992800, "from": {"id": 10002},
                                      "chat": {"id": 10002, "type": "private"}, "text": "/new"}}
    await app.db.ingest("42", [u])
    assert await app.db.offset() == 501
    await app.db.close()
    app.db = await Store(app.s.database).open()
    app.dialog = Dialog(app.db, app.s, "42")
    await app.dialog.process(500)
    d = (await app.db.query("SELECT * FROM drafts"))[0]
    assert d["step"] == "message"
    await app.dialog.process(500)
    assert len(await app.db.query("SELECT * FROM drafts")) == 1
    await app.db.execute("UPDATE drafts SET updated_at='2020-01-01T00:00:00+00:00'")
    # Helpers close over original store, so test recovered service directly.
    u["update_id"] = 501
    u["message"]["text"] = "/start"
    await app.db.ingest("42", [u])
    await app.dialog.process(501)
    assert (await app.db.query("SELECT * FROM drafts"))[0]["paused"] == 1
    await app.db.close()


@pytest.mark.parametrize("step", ["parent", "contact_type", "phone", "username", "extra", "messengers", "preference", "child_name", "child_age", "children", "comment_choice", "comment", "review", "edit", "title"])
async def test_menu_start_cancel_new_all_states(app, step):
    await app.intake(confirm=False)
    await app.db.execute("UPDATE drafts SET step=?", (step,))
    before = (await app.draft())["data"]
    await app.event("/start")
    assert (await app.draft())["data"] == before
    await app.event(action="menu:continue")
    assert (await app.draft())["step"] == step
    await app.event("/menu")
    await app.event(action="menu:new")
    assert (await app.draft())["step"] == "message"
    assert (await app.draft())["data"] == before
    await app.event("/cancel")
    assert await app.draft() is None
    await app.event("/new")
    assert (await app.draft())["step"] == "message"


async def test_edit_children_comment_and_contacts(app):
    await app.intake(kids=(("Алиса", "9"), ("Денис", "8")), confirm=False)
    await app.event(action="edit")
    await app.event(action="ename1")
    await app.event("Денис Александрович")
    assert (await app.draft())["data"]["children"][1]["name"] == "Денис Александрович"
    await app.event(action="edit")
    await app.event(action="delchild0")
    assert len((await app.draft())["data"]["children"]) == 1
    await app.event(action="delcomment")
    assert not (await app.draft())["data"]["comment"]
    await app.event(action="econtacts")
    await app.event(action="username")
    await app.event("@another_parent")
    await app.event(action="next")
    await app.event(action="pref0")
    assert (await app.draft())["step"] == "review"
    assert not (await app.draft())["data"]["phones"]


async def test_unauthorized_id_group_and_private_history(app):
    await app.intake()
    await app.event("/new", uid=9999)
    assert not await app.draft(9999)
    await app.event("/id", uid=9999)
    payload = json.loads((await app.db.query("SELECT payload FROM outbox WHERE kind IN ('send','ui') ORDER BY id DESC LIMIT 1"))[0]["payload"])
    assert payload["text"] == "Ваш Telegram ID: 9999"
    await app.event("/new", uid=10003, chat_type="group")
    assert not await app.draft(10003)
    ident = (await app.db.query("SELECT id FROM requests"))[0]["id"]
    await app.event("/request " + ident, uid=10003)
    response = json.loads((await app.db.query("SELECT payload FROM outbox WHERE kind IN ('send','ui') ORDER BY id DESC LIMIT 1"))[0]["payload"])
    assert response["text"] == "Только для администратора"
    await app.event(action="menu:mine", uid=10003)
    response = json.loads((await app.db.query("SELECT payload FROM outbox WHERE kind IN ('send','ui') ORDER BY id DESC LIMIT 1"))[0]["payload"])
    assert response["text"] == "Заявок пока нет"


@pytest.mark.parametrize("value,expected", [("8 (999) 123-45-67", "+79991234567"), ("+44 20 7946 0958", "+442079460958")])
def test_normalize_phones(value, expected):
    assert phone(value) == expected


def test_names_usernames_and_moscow():
    assert name("  Анна-Мария O’Connor <&>  ") == "Анна-Мария O’Connor <&>"
    assert username("HTTPS://T.ME/Some_Name") == "@some_name"
    assert moscow("2026-10-02T22:30:00+00:00") == "03.10.2026 01:30:00 МСК"
