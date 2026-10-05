"""Persistent numeric-ID grants and explicit approval of username candidates."""
import uuid
from datetime import datetime, timedelta, timezone
from .domain import InputError, now, username
from .storage import enqueue, one, rows


class Members:
    def __init__(self, settings, bot_id):
        self.s, self.bot = settings, str(bot_id)
        self.branch = settings.branch['key']

    async def seed(self, c):
        key = f'promoters_seeded:{self.bot}:{self.branch}'
        if not await one(c, 'SELECT value FROM metadata WHERE key=?', (key,)):
            for uid in self.s.promoters:
                await self.grant(c, uid, self.s.admin)
            await c.execute('INSERT INTO metadata VALUES(?,?)', (key, now()))

    async def authorized(self, c, uid):
        if self.s.is_admin(uid):
            return True
        return bool(await one(c, 'SELECT user_id FROM promoters WHERE bot_id=? AND branch_key=? AND user_id=? AND active=1',
                              (self.bot, self.branch, uid)))

    async def audit(self, c, actor, uid, action):
        await c.execute('INSERT INTO membership_audit(bot_id,branch_key,actor_id,target_id,action,created_at) VALUES(?,?,?,?,?,?)',
                        (self.bot, self.branch, actor, uid, action, now()))

    async def grant(self, c, uid, actor):
        if self.s.is_admin(uid):
            return False
        old = await one(c, 'SELECT active FROM promoters WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
        if old and old['active']:
            return False
        await c.execute('''INSERT INTO promoters VALUES(?,?,?,1,1,?,?)
            ON CONFLICT(bot_id,branch_key,user_id) DO UPDATE SET active=1,revision=revision+1,added_by=excluded.added_by,updated_at=excluded.updated_at''',
                        (self.bot, self.branch, uid, actor, now()))
        await self.audit(c, actor, uid, 'grant')
        return True

    def confirmation(self, invite):
        return (f'Аккаунт @{invite["username"]} связался с ботом. Telegram ID: {invite["candidate_id"]}.\nПодтвердить доступ промоутера?',
                [[('Подтвердить', f'admin:approve:{invite["id"]}:{invite["candidate_id"]}')],
                 [('Отменить приглашение', f'admin:cancel:{invite["id"]}')], [('Назад', 'admin:list')]])

    async def observe(self, c, sender):
        uid = sender.get('id')
        if not isinstance(uid, int) or uid <= 0 or sender.get('is_bot'):
            return
        try:
            handle = username(sender['username'])[1:] if sender.get('username') else None
        except InputError:
            handle = None
        await c.execute('UPDATE telegram_users SET username=NULL WHERE bot_id=? AND username=? AND user_id!=?', (self.bot, handle, uid))
        await c.execute('''INSERT INTO telegram_users VALUES(?,?,?,?) ON CONFLICT(bot_id,user_id)
            DO UPDATE SET username=excluded.username,seen_at=excluded.seen_at''', (self.bot, uid, handle, now()))
        if not handle:
            return
        invite = await one(c, "SELECT * FROM promoter_invites WHERE bot_id=? AND branch_key=? AND username=? AND state IN ('waiting','approval')",
                           (self.bot, self.branch, handle))
        if invite and invite['candidate_id'] != uid:
            await c.execute("UPDATE promoter_invites SET candidate_id=?,state='approval' WHERE id=?", (uid, invite['id']))
            invite['candidate_id'] = uid
            text, keyboard = self.confirmation(invite)
            await enqueue(c, f'invite:{invite["id"]}:{uid}', invite['created_by'], text, keyboard)

    async def list(self, c, reply, page=0):
        if not 0 <= page <= 100000:
            raise InputError("Неверная страница.")
        found = await rows(c, '''SELECT p.user_id,p.revision,u.username FROM promoters p LEFT JOIN telegram_users u
            ON u.bot_id=p.bot_id AND u.user_id=p.user_id WHERE p.bot_id=? AND p.branch_key=? AND p.active=1 ORDER BY p.user_id LIMIT 21 OFFSET ?''', (self.bot, self.branch, page * 20))
        pending = await rows(c, "SELECT * FROM promoter_invites WHERE bot_id=? AND branch_key=? AND state IN ('waiting','approval') ORDER BY created_at LIMIT 21 OFFSET ?", (self.bot, self.branch, page * 20))
        more = len(found) > 20 or len(pending) > 20
        found, pending = found[:20], pending[:20]
        text = 'Промоутеры:\n' + ('\n'.join(f'{p["user_id"]}' + (f' — @{p["username"]}' if p['username'] else '') for p in found) or 'Пока нет.')
        buttons = [[('Добавить промоутера', 'admin:add')]]
        for p in found:
            buttons.append([(f'Отключить {p["user_id"]}', f'admin:remove:{p["user_id"]}:{p["revision"]}')])
        for p in pending:
            text += f'\n@{p["username"]}: ' + ('ожидает /start' if p['state'] == 'waiting' else f'ожидает подтверждения ID {p["candidate_id"]}')
            if p['candidate_id']:
                buttons.append([(f'Подтвердить @{p["username"]}', f'admin:approve:{p["id"]}:{p["candidate_id"]}')])
            buttons.append([(f'Отменить @{p["username"]}', f'admin:cancel:{p["id"]}')])
        if page:
            buttons.append([('Предыдущая страница', f'admin:list:{page - 1}')])
        if more:
            buttons.append([('Следующая страница', f'admin:list:{page + 1}')])
        buttons += [[('Главное меню', 'menu:home')]]
        await reply(text, buttons)

    async def add(self, c, actor, value, reply):
        value = value.strip()
        if value.isascii() and value.isdecimal():
            uid = int(value)
            if uid <= 0 or uid > 2**52 - 1:
                raise InputError('Введите положительный Telegram ID или @username.')
            added = await self.grant(c, uid, actor)
            await reply(f'Промоутер {uid} добавлен. Ему нужно нажать /start.' if added else 'Этот пользователь уже имеет доступ.', [[('Промоутеры', 'admin:list')]])
        else:
            handle = username(value)[1:]
            invite = await one(c, "SELECT * FROM promoter_invites WHERE bot_id=? AND branch_key=? AND username=? AND state IN ('waiting','approval')", (self.bot, self.branch, handle))
            if not invite:
                ident = uuid.uuid4().hex[:16]
                await c.execute("INSERT INTO promoter_invites(id,bot_id,branch_key,username,created_by,created_at) VALUES(?,?,?,?,?,?)", (ident, self.bot, self.branch, handle, actor, now()))
                invite = await one(c, 'SELECT * FROM promoter_invites WHERE id=?', (ident,))
            known = await one(c, 'SELECT user_id FROM telegram_users WHERE bot_id=? AND username=?', (self.bot, handle))
            if known:
                invite['candidate_id'] = known['user_id']
                await c.execute("UPDATE promoter_invites SET state='approval',candidate_id=? WHERE id=?", (known['user_id'], invite['id']))
                await reply(*self.confirmation(invite))
            else:
                await reply(f'Приглашение для @{handle} сохранено. Пусть человек нажмёт /start в боте. После этого подтвердите его аккаунт.', [[('Промоутеры', 'admin:list')]])
        await c.execute('DELETE FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, actor))

    async def handle(self, c, uid, text, action, reply):
        session = await one(c, 'SELECT * FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
        requested = action.startswith('admin:') or text.split(' ', 1)[0] in ('/promoters', '/add_promoter')
        if not self.s.is_admin(uid):
            if requested:
                await reply('Только для администратора.')
            return requested
        if not requested and (action or text.startswith('/')):
            await c.execute('DELETE FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
            return False
        if not requested and not session:
            return False
        if not requested and datetime.fromisoformat(session['updated_at']) < datetime.now(timezone.utc) - timedelta(minutes=15):
            await c.execute('DELETE FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
            await reply('Время добавления истекло. Откройте «Промоутеры» заново.')
            return True
        try:
            if session and not requested:
                await self.add(c, uid, text, reply)
            elif text.startswith('/add_promoter '):
                await self.add(c, uid, text.split(' ', 1)[1], reply)
            elif action == 'admin:add' or text == '/add_promoter':
                await c.execute('INSERT OR REPLACE INTO admin_sessions VALUES(?,?,?,?,?)', (self.bot, self.branch, uid, 'add', now()))
                await reply('Введите Telegram ID или @username промоутера. Для username потребуется /start и подтверждение аккаунта.', [[('Отмена', 'admin:list')]])
            elif action.startswith('admin:approve:'):
                _, _, ident, candidate = action.split(':')
                invite = await one(c, "SELECT * FROM promoter_invites WHERE id=? AND bot_id=? AND branch_key=? AND state='approval' AND candidate_id=?", (ident, self.bot, self.branch, int(candidate)))
                known = await one(c, 'SELECT user_id FROM telegram_users WHERE bot_id=? AND user_id=? AND username=?', (self.bot, int(candidate), invite['username'])) if invite else None
                if not known:
                    await reply('Приглашение устарело или username изменился. Проверьте список промоутеров.')
                else:
                    await self.grant(c, int(candidate), uid)
                    await c.execute("UPDATE promoter_invites SET state='approved' WHERE id=?", (ident,))
                    await reply(f'Доступ промоутера подтверждён для ID {candidate}.', [[('Промоутеры', 'admin:list')]])
            elif action.startswith('admin:cancel:'):
                ident = action.split(':')[2]
                await c.execute("UPDATE promoter_invites SET state='cancelled' WHERE id=? AND bot_id=? AND branch_key=? AND state IN ('waiting','approval')", (ident, self.bot, self.branch))
                await self.list(c, reply)
            elif action.startswith(('admin:remove:', 'admin:revoke:')):
                _, command, target, revision = action.split(':')
                member = await one(c, 'SELECT * FROM promoters WHERE bot_id=? AND branch_key=? AND user_id=? AND revision=? AND active=1', (self.bot, self.branch, int(target), int(revision)))
                if not member or self.s.is_admin(int(target)):
                    await reply('Эта кнопка устарела или пользователь — администратор.')
                elif command == 'remove':
                    await reply(f'Отключить доступ промоутера {target}? Сохранённые заявки останутся.', [[('Отключить', f'admin:revoke:{target}:{revision}')], [('Отмена', 'admin:list')]])
                else:
                    await c.execute('UPDATE promoters SET active=0,revision=revision+1,updated_at=? WHERE bot_id=? AND branch_key=? AND user_id=?', (now(), self.bot, self.branch, int(target)))
                    await self.audit(c, uid, int(target), 'revoke')
                    await self.list(c, reply)
            else:
                await c.execute('DELETE FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
                page = int(action.split(":")[2]) if action.startswith("admin:list:") else 0
                await self.list(c, reply, page)
        except (InputError, ValueError):
            await reply('Введите корректный Telegram ID, @username или ссылку t.me.', [[('Отмена', 'admin:list')]])
        return True
