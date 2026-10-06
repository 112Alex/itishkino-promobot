"""Explicit synthetic end-to-end acceptance; never used by ordinary startup."""
import json
import uuid
from .config import ConfigurationError
from .dialog import Dialog
from .storage import dumps
from .worker import Worker


async def questionnaire(db, settings, bot_id):
    dialog = Dialog(db, settings, bot_id)
    uid = settings.admin
    offset = await db.offset()
    async def send(text=None, action=None):
        nonlocal offset
        offset += 1
        message = {"message_id": offset, "date": 1790992800, "chat": {"id": uid, "type": "private"},
                   "from": {"id": uid, "is_bot": False, "first_name": "Приёмка"}}
        if action:
            if action.startswith("menu:"):
                callback = action
            else:
                ds = await db.query("SELECT id,version FROM drafts WHERE bot_id=? AND user_id=? AND active=1", (bot_id, uid))
                callback = f'd:{ds[0]["id"]}:{ds[0]["version"]}:{action}'
            u = {"update_id": offset, "callback_query": {"id": f"demo{offset}", "from": message["from"], "message": message, "data": callback}}
        else:
            u = {"update_id": offset, "message": {**message, "text": text}}
        await db.ingest(bot_id, [u])
        await dialog.process(offset)
    await send(action="menu:new")
    drafts = await db.query("SELECT step FROM drafts WHERE bot_id=? AND user_id=? AND active=1", (bot_id, uid))
    if drafts[0]["step"] == "replace":
        raise ConfigurationError("Для приёмки нужна БД без активного черновика владельца")
    if drafts[0]["step"] == "branch":
        await send(action="branch." + settings.branch["key"])
    await send("Тест промобота")
    await send(action="username")
    await send("@promo_test_" + uuid.uuid4().hex[:16])
    await send(action="next")
    await send(action="pref0")
    await send("Алиса")
    await send("9")
    await send(action="next")
    await send(action="comment")
    await send("Вымышленная семья. Проверка интеграции; не связываться.")
    await send(action="confirm")
    return (await db.query("SELECT id FROM requests ORDER BY saved_at DESC LIMIT 1"))[0]["id"]


async def acceptance(db, settings, crm, allow_real=False):
    if settings.mode == "real":
        if settings.environment != "test" or not allow_real:
            raise ConfigurationError("Реальная приёмка только ENVIRONMENT=test с --allow-real-write и отдельной БД")
        settings.require_real_contract()
    if await db.query("SELECT id FROM requests WHERE state NOT IN ('delivered','duplicate_review') LIMIT 1"):
        raise ConfigurationError("Для приёмки используйте отдельную БД без незавершённых задач")
    bot_id = str(settings.token.split(":")[0]) if settings.token else "demo"
    await db.bind(settings.environment, bot_id, settings.branch, settings.mode, settings.crm_url, settings.all_branches)
    ident = await questionnaire(db, settings, bot_id)
    worker = Worker(db, settings, crm)
    await worker.recover()
    await worker.tick()
    r = (await db.query("SELECT id,state,error,crm_id FROM requests WHERE id=?", (ident,)))[0]
    print(dumps({"mode": settings.mode, "result": r,
                 "telegram_send": "не выполнен: ответы только в outbox", "real_ui_acceptance": "требуется просмотр карточки владельцем"}))
    if r["state"] != "delivered":
        raise ConfigurationError("Приёмка не завершена; проверьте doctor и состояние заявки")
