import json
import uuid
from datetime import datetime, timezone, timedelta
from .domain import InputError, age, clean, family, name, now, phone, review, title, username
from .storage import dumps, enqueue, one, rows

LABELS = {"queued": "В очереди", "checking": "Проверяется", "creating": "Создаётся",
          "crm_created": "Карточка создана, проверяем", "comment_pending": "Сохраняется комментарий",
          "verifying": "Сверка результата", "delivered": "Создано в CRM",
          "duplicate_review": "Совпадение — у администратора", "retry_wait": "Ожидание повтора",
          "manual_review": "Нужна проверка администратора", "failed": "Ошибка"}
PREFERENCES = ["Только писать", "Только звонить", "Можно оба способа", "Не уточнили"]
MESSENGERS = ["MAX", "Telegram", "WhatsApp"]
HELP = ("Одна анкета — одна семья. Добавляйте всех детей. Телефон или Telegram обязателен. "
        "Комментарий один, его можно исправить перед отправкой. «Заявка сохранена» означает приём ботом; "
        "«Создано в CRM» приходит отдельно. До подтверждения доступны Назад, Отмена, Главное меню. "
        "После отправки исправления — через администратора. При отключении дольше суток Telegram "
        "может потерять ещё не принятые сообщения; проверьте ответ о сохранении.")


class Dialog:
    def __init__(self, store, settings, bot_id):
        self.db, self.s, self.bot_id = store, settings, str(bot_id)

    def menu(self, has_draft=False, admin=False):
        result = [[("Добавить лида", "menu:new")]]
        if has_draft:
            result.append([("Продолжить анкету", "menu:continue")])
        result += [[("Мои заявки", "menu:mine")], [("Помощь", "menu:help")]]
        if admin:
            result.append([("Проблемные заявки", "menu:problems")])
        return result

    def buttons(self, d, options):
        buttons = [[(label, f'd:{d["id"]}:{d["version"]}:{action}')] for label, action in options]
        buttons += [[("Назад", f'd:{d["id"]}:{d["version"]}:back'),
                     ("Отмена", f'd:{d["id"]}:{d["version"]}:cancel')],
                    [("Главное меню", "menu:home")]]
        return buttons

    def screen(self, d):
        data, step = d["data"], d["step"]
        options = []
        prompts = {
            "parent": "Имя родителя (до 50 символов):", "phone": "Введите телефон с кодом страны:",
            "username": "Введите @username или ссылку t.me:", "child_name": "Имя ребёнка:",
            "child_age": "Возраст ребёнка, полных лет:", "comment": "Введите один комментарий для всей семьи:",
            "title": f'Полный заголовок не помещается в CRM ({self.s.name_limit} символов). Введите сокращённый заголовок; полный состав семьи останется в примечании:',
            "contact_type": "Выберите основной контакт:", "extra": "Добавить дополнительный контакт?",
            "messengers": "Какие мессенджеры доступны по телефону? Выберите несколько, затем «Дальше».",
            "preference": "Как связаться?", "children": "Добавить ещё ребёнка?",
            "comment_choice": "Добавить комментарий?", "edit": "Выберите, что исправить:",
            "replace": "Уже есть черновик. Продолжить или начать заново?"}
        if step == "contact_type":
            options = [("Телефон", "phone"), ("Telegram @username", "username")]
        elif step == "extra":
            options = [("Добавить телефон", "phone"), ("Добавить Telegram", "username"), ("Дальше", "next")]
        elif step == "messengers":
            options = [(('✓ ' if m in data.get("messengers", []) else '') + m, f'msg{n}') for n, m in enumerate(MESSENGERS)]
            options += [("Только телефон / не указано", "none"), ("Дальше", "next")]
        elif step == "preference":
            options = [(p, f'pref{n}') for n, p in enumerate(PREFERENCES) if data.get("phones") or n in {0, 3}]
        elif step == "child_age":
            options = [("Не уточнили", "unknown")]
        elif step == "children":
            options = [("Добавить ребёнка", "addchild"), ("Дальше", "next")]
        elif step == "comment_choice":
            options = [("Добавить комментарий", "comment"), ("Без комментария", "skip")]
        elif step == "review":
            return review(data, self.s.branch), self.buttons(d, [("Отправить в CRM", "confirm"), ("Исправить", "edit")])
        elif step == "edit":
            options = [("Имя родителя", "eparent"), ("Контакты (ввести заново)", "econtacts"),
                       ("Способы связи", "emessengers"), ("Предпочтение связи", "epreference"),
                       ("Добавить ребёнка", "eadd"), ("Комментарий", "ecomment"),
                       ("Удалить комментарий", "delcomment"), ("Заголовок", "etitle"), ("К проверке", "review")]
            for n, child in enumerate(data["children"]):
                options += [(f'{n+1}. Исправить имя', f'ename{n}'), (f'{n+1}. Исправить возраст', f'eage{n}')]
                if len(data["children"]) > 1:
                    options.append((f'{n+1}. Удалить ребёнка', f'delchild{n}'))
        elif step == "replace":
            options = [("Продолжить", "resume"), ("Начать заново", "restart")]
        return prompts.get(step, "Продолжите анкету"), self.buttons(d, options)

    async def new(self, c, uid, chat):
        stamp = now()
        ident = uuid.uuid4().hex[:12]
        await c.execute("INSERT INTO drafts(id,bot_id,user_id,chat_id,branch_key,crm_branch_id,step,version,data,history,created_at,updated_at) VALUES(?,?,?,?,?,?,'parent',1,?,'[]',?,?)",
                        (ident, self.bot_id, uid, chat, self.s.branch["key"], self.s.branch["crm_id"],
                         dumps({"children": [], "phones": [], "usernames": [], "messengers": []}), stamp, stamp))
        return await self.draft(c, uid, chat)

    async def draft(self, c, uid, chat):
        d = await one(c, "SELECT * FROM drafts WHERE bot_id=? AND user_id=? AND chat_id=? AND active=1", (self.bot_id, uid, chat))
        if d:
            d["data"], d["history"] = json.loads(d["data"]), json.loads(d["history"])
        return d

    async def save(self, c, d):
        await c.execute("UPDATE drafts SET step=?,version=?,data=?,history=?,paused=?,updated_at=?,first_message_at=?,first_received_at=? WHERE id=? AND active=1",
                        (d["step"], d["version"], dumps(d["data"]), dumps(d["history"]), d["paused"],
                         now(), d["first_message_at"], d["first_received_at"], d["id"]))

    async def process(self, update_id):
        # No external I/O in this transaction. All responses are durable intents.
        async with self.db.tx() as c:
            event = await one(c, "SELECT * FROM inbox WHERE bot_id=? AND update_id=? AND state='pending'", (self.bot_id, update_id))
            if not event:
                return
            u = json.loads(event["payload"])
            uid, chat = event["user_id"], event["chat_id"]
            q = u.get("callback_query")
            message = u.get("message") or (q or {}).get("message") or {}
            text = message.get("text", "") if not q else ""
            action = q.get("data", "") if q else ""
            key = f'in:{self.bot_id}:{update_id}'
            async def reply(value, keyboard=None):
                # Plain text mode. No HTML interpretation in Telegram.
                await enqueue(c, key + ":reply", chat, value, keyboard)
            if q:
                await enqueue(c, key + ":ack", chat, q["id"], kind="ack")
            private = message.get("chat", {}).get("type") == "private"
            if text.split("@", 1)[0] == "/id" and uid:
                await reply(f"Ваш Telegram ID: {uid}")
            elif not private or not self.s.authorized(uid):
                if private and chat:
                    await reply("Доступ не разрешён. Отправьте свой /id владельцу.")
            else:
                d = await self.draft(c, uid, chat)
                stale = bool(d and (d["data"].get("_stale") or datetime.fromisoformat(d["updated_at"]) < datetime.now(timezone.utc) - timedelta(hours=24)))
                if stale:
                    d["data"]["_stale"] = True
                if text == "/start":
                    action = "menu:home"
                elif text == "/new":
                    action = "menu:new"
                elif text == "/cancel":
                    action = "cancel"
                elif text == "/back":
                    action = "back"
                elif text == "/menu":
                    action = "menu:home"
                if (text.startswith("/request ") or text.startswith("/retry ")):
                    await self.admin_request(c, uid, text, reply)
                elif text == "/problems":
                    await self.problems(c, uid, reply)
                elif action.startswith("menu:"):
                    command = action.split(":")[1]
                    if command == "new":
                        if d:
                            d["data"]["resume_step"] = d["data"].get("resume_step", d["step"])
                            d["step"], d["paused"] = "replace", 0
                            d["version"] += 1
                            await self.save(c, d)
                        else:
                            d = await self.new(c, uid, chat)
                        await reply(*self.screen(d))
                    elif command == "continue" and d:
                        d["paused"] = 0
                        d["version"] += 1
                        if stale and d["step"] != "replace":
                            d["data"]["resume_step"] = d["step"]
                            d["step"] = "replace"
                        await self.save(c, d)
                        value, keyboard = self.screen(d)
                        await reply(("Черновик старше суток. Проверьте актуальность ответов.\n" if stale else "") + value, keyboard)
                    elif command == "mine":
                        found = await rows(c, "SELECT id,state FROM requests WHERE bot_id=? AND user_id=? ORDER BY saved_at DESC LIMIT 10", (self.bot_id, uid))
                        await reply("\n".join(f'{r["id"]}: {LABELS.get(r["state"], r["state"])}' for r in found) or "Заявок пока нет", self.menu(bool(d), self.s.is_admin(uid)))
                    elif command == "help":
                        await reply(HELP, self.menu(bool(d), self.s.is_admin(uid)))
                    elif command == "problems":
                        await self.problems(c, uid, reply)
                    else:
                        if d:
                            d["paused"] = 1
                            d["version"] += 1
                            await self.save(c, d)
                        await reply("Главное меню. Черновик сохранён." if d else "Главное меню", self.menu(bool(d), self.s.is_admin(uid)))
                else:
                    valid = True
                    if q:
                        parts = action.split(":")
                        valid = len(parts) == 4 and parts[0] == "d" and d and parts[1] == d["id"] and parts[2] == str(d["version"]) and not d["paused"]
                        action = parts[3] if valid else ""
                    if not valid:
                        await reply("Эта кнопка устарела или принадлежит другой анкете. Откройте /start.")
                    elif not d or d["paused"]:
                        await reply("Откройте /start и продолжите черновик или создайте новый контакт", self.menu(bool(d), self.s.is_admin(uid)))
                    elif stale and action not in {"cancel", "back", "resume", "restart"}:
                        if d["step"] != "replace":
                            d["data"]["resume_step"] = d["step"]
                        d["step"], d["paused"] = "replace", 0
                        d["version"] += 1
                        await self.save(c, d)
                        value, buttons = self.screen(d)
                        await reply("Черновик старше суток. Проверьте актуальность ответов.\n" + value, buttons)
                    elif action == "cancel":
                        await c.execute("UPDATE drafts SET active=0,step='cancelled',version=version+1 WHERE id=?", (d["id"],))
                        await reply("Анкета отменена", self.menu(False, self.s.is_admin(uid)))
                    elif action == "restart" and d["step"] == "replace":
                        await c.execute("UPDATE drafts SET active=0,step='cancelled' WHERE id=?", (d["id"],))
                        d = await self.new(c, uid, chat)
                        await reply(*self.screen(d))
                    elif action == "confirm" and d["step"] == "review":
                        await self.confirm(c, d, message, reply)
                    else:
                        # Validate on a copy; rejected input cannot partially modify a draft.
                        proposed = json.loads(dumps(d))
                        try:
                            self.advance(proposed, action, text)
                            if not proposed["first_message_at"] and text and not text.startswith("/"):
                                proposed["first_message_at"] = datetime.fromtimestamp(message.get("date", 0), timezone.utc).isoformat()
                                proposed["first_received_at"] = event["received_at"]
                            proposed["version"] += 1
                            await self.save(c, proposed)
                            await reply(*self.screen(proposed))
                        except InputError as exc:
                            question, buttons = self.screen(d)
                            await reply(f"{exc}\n{question}", buttons)
            await c.execute("UPDATE inbox SET state='done' WHERE bot_id=? AND update_id=?", (self.bot_id, update_id))

    def advance(self, d, action, text):
        data, step = d["data"], d["step"]
        if action == "back":
            if d["history"]:
                old = d["history"].pop()
                d["step"], d["data"] = old["step"], old["data"]
            return
        d["history"].append({"step": step, "data": json.loads(dumps(data))})
        def finish(next_step):
            if data.pop("editing", False):
                d["step"] = "review"
                data.pop("edit_index", None)
                if next_step != "title":
                    data.pop("title", None)
                self.ensure_title(d)
            else:
                d["step"] = next_step
        if step == "replace" and action == "resume":
            d["step"] = data.pop("resume_step", "parent")
            data.pop("_stale", None)
        elif step == "parent":
            data["parent"] = name(text, 50)
            finish("contact_type")
        elif step in {"contact_type", "extra"}:
            if action in {"phone", "username"}:
                d["step"] = action
            elif step == "extra" and action == "next":
                if not data.get("phones") and not data.get("usernames"):
                    raise InputError("Нужен хотя бы один контакт")
                d["step"] = "messengers" if data.get("phones") else "preference"
            else:
                raise InputError("Выберите контакт кнопкой")
        elif step in {"phone", "username"}:
            field = "phones" if step == "phone" else "usernames"
            value = phone(text) if step == "phone" else username(text)
            if len(data[field]) >= 2 and value not in data[field]:
                raise InputError("До двух контактов каждого типа. Исправьте контакты через проверку")
            if value not in data[field]:
                data[field].append(value)
            d["step"] = "extra"
        elif step == "messengers":
            if action.startswith("msg") and action[3:].isdigit() and int(action[3:]) < len(MESSENGERS):
                m = MESSENGERS[int(action[3:])]
                data["messengers"] = [x for x in data["messengers"] if x != m] if m in data["messengers"] else data["messengers"] + [m]
            elif action in {"none", "next"}:
                if action == "none":
                    data["messengers"] = []
                if data.get("editing"):
                    finish("preference")
                else:
                    d["step"] = "preference"
            else:
                raise InputError("Выберите мессенджеры кнопками")
        elif step == "preference":
            if not action.startswith("pref") or action[4:] not in {"0", "1", "2", "3"}:
                raise InputError("Выберите предпочтение кнопкой")
            n = int(action[4:])
            if not data.get("phones") and n in {1, 2}:
                raise InputError("Без телефона доступны только переписка и «Не уточнили»")
            data["preference"] = PREFERENCES[n]
            finish("child_name" if not data["children"] else "review")
        elif step == "child_name":
            value = name(text)
            if "edit_index" in data:
                data["children"][data["edit_index"]]["name"] = value
                finish("review")
            else:
                data["pending_child"] = value
                d["step"] = "child_age"
        elif step == "child_age":
            value = age("Не уточнили" if action == "unknown" else text, self.s)
            if "edit_index" in data:
                data["children"][data["edit_index"]]["age"] = value
                finish("review")
            else:
                data["children"].append({"name": data.pop("pending_child"), "age": value})
                finish("children")
        elif step == "children":
            if action == "addchild":
                if len(data["children"]) >= 12:
                    raise InputError("В одной заявке до 12 детей; обратитесь к администратору")
                d["step"] = "child_name"
            elif action == "next":
                d["step"] = "comment_choice"
            else:
                raise InputError("Нажмите «Добавить ребёнка» или «Дальше»")
        elif step == "comment_choice":
            if action == "comment":
                d["step"] = "comment"
            elif action == "skip":
                data["comment"] = ""
                d["step"] = "review"
            else:
                raise InputError("Выберите кнопкой")
        elif step == "comment":
            value = clean(text, multiline=True)
            if not value or len(value) > self.s.comment_limit:
                raise InputError(f"Введите текст до {self.s.comment_limit} символов")
            data["comment"] = value
            finish("review")
        elif step == "title":
            data["title"] = name(text, self.s.name_limit)
            data.pop("editing", None)
            d["step"] = "review"
        elif step == "review" and action == "edit":
            d["step"] = "edit"
        elif step == "edit":
            fields = {"eparent": "parent", "econtacts": "contact_type", "emessengers": "messengers",
                      "epreference": "preference", "ecomment": "comment", "etitle": "title", "eadd": "child_name"}
            if action in fields:
                data["editing"] = True
                d["step"] = fields[action]
                if action == "econtacts":
                    data["phones"], data["usernames"], data["messengers"] = [], [], []
                if action == "emessengers" and not data.get("phones"):
                    d["step"] = "preference"
            elif action.startswith(("ename", "eage", "delchild")):
                prefix = "ename" if action.startswith("ename") else "eage" if action.startswith("eage") else "delchild"
                try:
                    n = int(action[len(prefix):])
                    if not 0 <= n < len(data["children"]):
                        raise ValueError
                except ValueError:
                    raise InputError("Ребёнок не найден") from None
                if prefix == "delchild":
                    if len(data["children"]) <= 1:
                        raise InputError("Должен остаться хотя бы один ребёнок")
                    data["children"].pop(n)
                    data.pop("title", None)
                else:
                    data["editing"], data["edit_index"] = True, n
                    d["step"] = "child_name" if prefix == "ename" else "child_age"
            elif action == "delcomment":
                data["comment"] = ""
            elif action == "review":
                d["step"] = "review"
            else:
                raise InputError("Выберите поле кнопкой")
        else:
            raise InputError("Ответьте текстом или используйте кнопки текущего шага")
        d["history"] = d["history"][-50:]
        self.ensure_title(d)

    def ensure_title(self, d):
        if d["step"] == "review" and len(d["data"].get("title") or title(d["data"])) > self.s.name_limit:
            d["step"] = "title"

    async def confirm(self, c, d, message, reply):
        data = d["data"]
        if not data["children"] or not (data.get("phones") or data.get("usernames")):
            raise InputError("Неполная анкета")
        data["crm_settings"] = {k: self.s.branch.get(k) for k in ("key", "crm_id", "pipeline_id", "status_id", "source_id", "request_field", "technical_user_id", "initial_unassigned")}
        ident = f'{self.s.environment}-{self.bot_id}-{self.s.branch["key"]}-{uuid.uuid4().hex}'
        stamp = now()
        await c.execute("INSERT INTO requests(id,draft_id,bot_id,user_id,chat_id,branch_key,crm_branch_id,data,state,confirmed_at,saved_at) VALUES(?,?,?,?,?,?,?,?,'queued',?,?)",
                        (ident, d["id"], self.bot_id, d["user_id"], d["chat_id"], d["branch_key"], d["crm_branch_id"], dumps(data), stamp, now()))
        await c.execute("INSERT INTO jobs(request_id) VALUES(?)", (ident,))
        await c.execute("UPDATE drafts SET active=0,step='submitted',version=version+1 WHERE id=?", (d["id"],))
        await reply(f"Заявка сохранена: {ident}. Доставка в CRM в очереди.", self.menu(False, self.s.is_admin(d["user_id"])))

    async def problems(self, c, uid, reply):
        if not self.s.is_admin(uid):
            return await reply("Только для администратора")
        found = await rows(c, "SELECT id,state,error FROM requests WHERE state IN ('manual_review','duplicate_review','failed','retry_wait') ORDER BY saved_at DESC LIMIT 20")
        await reply("\n".join(f'{r["id"]}: {LABELS[r["state"]]} ({r["error"] or "совпадение"})' for r in found) or "Проблемных заявок нет")

    async def admin_request(self, c, uid, text, reply):
        if not self.s.is_admin(uid):
            return await reply("Только для администратора")
        command, ident = text.split(maxsplit=1)
        r = await one(c, "SELECT * FROM requests WHERE id=?", (ident,))
        if not r:
            return await reply("Заявка не найдена")
        if command == "/retry":
            if r["state"] == "duplicate_review":
                return await reply("Совпадение запрещает автоматическое создание. Проверьте семью вручную в CRM.")
            if r["state"] not in {"manual_review", "failed", "retry_wait"}:
                return await reply("Повтор сейчас недоступен")
            # Preserve phase: ambiguous operations never revert to create.
            await c.execute("UPDATE jobs SET state='pending',attempts=0,next_at=0,error=NULL WHERE request_id=? AND state!='processing'", (ident,))
            await c.execute("UPDATE requests SET state='queued',error=NULL WHERE id=?", (ident,))
            return await reply("Повторная проверка запланирована. Неопределённую запись сначала сверим.")
        value = review(json.loads(r["data"]), self.s.branch)
        matches = json.loads(r["matches"] or "[]")
        links = ", ".join(str(x) for x in matches)
        await reply(f'{r["id"]}\n{LABELS.get(r["state"], r["state"])}\n{value}\nCRM ID: {r["crm_id"]}\nСовпадения: {links or "нет"}\nОшибка: {r["error"] or "нет"}')
