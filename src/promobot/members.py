"""Persistent numeric-ID grants and explicit approval of username candidates."""
import uuid
import json
from datetime import datetime, timedelta, timezone
from .domain import InputError, now, username, name, clean
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
        for uid in self.s.admin_ids:
            await c.execute('''INSERT OR IGNORE INTO administrators(bot_id,user_id,added_by,updated_at)
                VALUES(?,?,?,?)''', (self.bot, uid, self.s.admin, now()))

    async def is_admin(self, c, uid):
        return self.s.is_admin(uid) or bool(await one(c, 'SELECT user_id FROM administrators WHERE bot_id=? AND user_id=? AND active=1', (self.bot, uid)))

    @staticmethod
    def label(member, number=1):
        label = member.get('display_name') or member.get('full_name') or ('@' + member['username'] if member.get('username') else f'Промоутер №{number}')
        return label if len(label) <= 45 else label[:44] + '…'

    async def input_session(self, c, uid, step, target=None):
        await c.execute('''INSERT OR REPLACE INTO admin_sessions(bot_id,branch_key,user_id,step,updated_at,target_id)
            VALUES(?,?,?,?,?,?)''', (self.bot, self.branch, uid, step, now(), target))

    async def watch_screen(self, c, uid, reply):
        current = await one(c, 'SELECT branches FROM admin_watches WHERE bot_id=? AND user_id=?', (self.bot, uid))
        selected = json.loads(current['branches']) if current else list(self.s.all_branches)
        await reply('🔔 Выберите филиалы для уведомлений о заявках и ошибках доставки. Изменения сохраняются сразу.',
                    [[(('✅ ' if k in selected else '⬜ ') + b['name'], 'admin:watch:' + k)] for k, b in self.s.all_branches.items()] +
                    [[('↩️ Админское меню', 'admin:home')]])

    async def admin_list(self, c, reply, page=0):
        if not 0 <= page <= 100000:
            raise InputError('Неверная страница.')
        found = await rows(c, '''SELECT a.*,u.full_name,u.username FROM administrators a LEFT JOIN telegram_users u
            ON a.bot_id=u.bot_id AND a.user_id=u.user_id WHERE a.bot_id=? AND a.active=1 ORDER BY a.user_id LIMIT 21 OFFSET ?''', (self.bot, page*20))
        more, found = len(found) > 20, found[:20]
        await reply('🛡 Администраторы:\n' + '\n'.join(self.label(a, n + page*20 + 1).replace('Промоутер №', 'Администратор №') for n, a in enumerate(found)),
                    [[('➕ Добавить администратора', 'admin:addadmin')]] +
                    [[('✏️ ' + self.label(a, n + page*20 + 1).replace('Промоутер №', 'Администратор №'), 'admin:aname:' + str(a['user_id']))] for n, a in enumerate(found)] +
                    ([[('⬅️ Предыдущая страница', f'admin:admins:{page-1}')]] if page else []) +
                    ([[('➡️ Следующая страница', f'admin:admins:{page+1}')]] if more else []) +
                    [[('↩️ Админское меню', 'admin:home')]])

    async def management(self, c, uid, text, action, reply):
        session = await one(c, 'SELECT * FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
        managed = action.startswith(('admin:home', 'admin:admins', 'admin:addadmin', 'admin:watch', 'admin:name:', 'admin:aname:'))
        typed = bool(session and session['step'] != 'add' and not action and not text.startswith('/'))
        if not managed and not typed:
            return False
        if not await self.is_admin(c, uid):
            await reply('Только для администратора.')
            return True
        try:
            if managed:
                await c.execute('DELETE FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
            if typed:
                if datetime.fromisoformat(session['updated_at']) < datetime.now(timezone.utc) - timedelta(minutes=15):
                    await c.execute('DELETE FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
                    raise InputError('Время ввода истекло. Откройте меню заново.')
                if session['step'] == 'addadmin':
                    if not text.strip().isascii() or not text.strip().isdecimal() or not 0 < int(text) <= 2**52-1:
                        raise InputError('Введите положительный числовой Telegram ID.')
                    target = int(text)
                    if await self.is_admin(c, target):
                        await reply('Этот пользователь уже является администратором.', [[('🛡 Администраторы', 'admin:admins')]])
                        await c.execute('DELETE FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
                        return True
                    await c.execute('''INSERT INTO administrators(bot_id,user_id,added_by,updated_at) VALUES(?,?,?,?)
                        ON CONFLICT(bot_id,user_id) DO UPDATE SET active=1,revision=revision+1,updated_at=excluded.updated_at''', (self.bot, target, uid, now()))
                    await self.audit(c, uid, target, 'grant_admin')
                    await self.input_session(c, uid, 'name_admin', target)
                    await reply('Администратор добавлен. Введите его имя:', [[('↩️ Позже', 'admin:admins')]])
                elif session['step'] in ('name_admin', 'name_promoter'):
                    label = name(text, 70)
                    if session['step'] == 'name_admin':
                        await c.execute('UPDATE administrators SET display_name=?,updated_at=? WHERE bot_id=? AND user_id=? AND active=1', (label, now(), self.bot, session['target_id']))
                    else:
                        await c.execute('UPDATE promoters SET display_name=?,updated_at=? WHERE bot_id=? AND branch_key=? AND user_id=? AND active=1', (label, now(), self.bot, self.branch, session['target_id']))
                    await self.audit(c, uid, session['target_id'], 'rename')
                    await c.execute('DELETE FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
                    await (self.admin_list(c, reply) if session['step'] == 'name_admin' else self.list(c, reply))
            elif action == 'admin:home':
                await reply('⚙️ Админское меню', [[('👥 Промоутеры', 'admin:list')], [('🛡 Администраторы', 'admin:admins')],
                    [('🔔 Мои филиалы для уведомлений', 'admin:watch')], [('⚠️ Проблемные заявки', 'menu:problems')], [('🏠 Главное меню', 'menu:home')]])
            elif action == 'admin:admins' or action.startswith('admin:admins:'):
                await self.admin_list(c, reply, int(action.split(':')[2]) if action.count(':') == 2 else 0)
            elif action == 'admin:addadmin':
                await self.input_session(c, uid, 'addadmin')
                await reply('Введите Telegram ID нового администратора. Он получит доступ ко всем филиалам и сможет добавлять администраторов.', [[('↩️ Отмена', 'admin:admins')]])
            elif action.startswith('admin:watch'):
                if action.startswith('admin:watch:'):
                    key = action.split(':')[2]
                    if key not in self.s.all_branches:
                        raise InputError('Филиал недоступен.')
                    old = await one(c, 'SELECT branches FROM admin_watches WHERE bot_id=? AND user_id=?', (self.bot, uid))
                    selected = set(json.loads(old['branches']) if old else self.s.all_branches)
                    selected.symmetric_difference_update({key})
                    await c.execute('INSERT OR REPLACE INTO admin_watches VALUES(?,?,?)', (self.bot, uid, json.dumps(sorted(selected))))
                await self.watch_screen(c, uid, reply)
            elif action.startswith(('admin:name:', 'admin:aname:')):
                target = int(action.split(':')[2])
                role = 'admin' if action.startswith('admin:aname:') else 'promoter'
                table = 'administrators' if role == 'admin' else 'promoters'
                member = await one(c, f'SELECT user_id FROM {table} WHERE bot_id=? AND user_id=? AND active=1', (self.bot, target))
                if not member:
                    raise InputError('Пользователь недоступен. Откройте список заново.')
                await self.input_session(c, uid, 'name_' + role, target)
                await reply('Введите имя для отображения в списке:', [[('↩️ Отмена', 'admin:admins' if role == 'admin' else 'admin:list')]])
        except (InputError, ValueError) as exc:
            await reply(str(exc) if isinstance(exc, InputError) else 'Некорректный ввод.', [[('↩️ Админское меню', 'admin:home')]])
        return True

    async def authorized(self, c, uid):
        if await self.is_admin(c, uid):
            return True
        return bool(await one(c, 'SELECT user_id FROM promoters WHERE bot_id=? AND branch_key=? AND user_id=? AND active=1',
                              (self.bot, self.branch, uid)))

    async def audit(self, c, actor, uid, action):
        await c.execute('INSERT INTO membership_audit(bot_id,branch_key,actor_id,target_id,action,created_at) VALUES(?,?,?,?,?,?)',
                        (self.bot, self.branch, actor, uid, action, now()))

    async def grant(self, c, uid, actor):
        if await self.is_admin(c, uid):
            return False
        old = await one(c, 'SELECT active FROM promoters WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
        if old and old['active']:
            return False
        await c.execute('''INSERT INTO promoters(bot_id,branch_key,user_id,active,revision,added_by,updated_at) VALUES(?,?,?,1,1,?,?)
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
        await c.execute('''INSERT INTO telegram_users(bot_id,user_id,username,seen_at,full_name) VALUES(?,?,?,?,?) ON CONFLICT(bot_id,user_id)
            DO UPDATE SET username=excluded.username,seen_at=excluded.seen_at,full_name=excluded.full_name''', (self.bot, uid, handle, now(), clean(' '.join(str(sender.get(k, '')) for k in ('first_name','last_name')))[:100] or None))
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
        found = await rows(c, '''SELECT p.user_id,p.revision,p.display_name,u.username,u.full_name FROM promoters p LEFT JOIN telegram_users u
            ON u.bot_id=p.bot_id AND u.user_id=p.user_id WHERE p.bot_id=? AND p.branch_key=? AND p.active=1 ORDER BY p.user_id LIMIT 21 OFFSET ?''', (self.bot, self.branch, page * 20))
        pending = await rows(c, "SELECT * FROM promoter_invites WHERE bot_id=? AND branch_key=? AND state IN ('waiting','approval') ORDER BY created_at LIMIT 21 OFFSET ?", (self.bot, self.branch, page * 20))
        more = len(found) > 20 or len(pending) > 20
        found, pending = found[:20], pending[:20]
        text = '👥 Промоутеры:\n' + ('\n'.join(self.label(p, n + page * 20 + 1) for n, p in enumerate(found)) or 'Пока нет.')
        buttons = [[('➕ Добавить промоутера', 'admin:add')]]
        for n, p in enumerate(found):
            label = self.label(p, n + page * 20 + 1)
            buttons.append([(f'✏️ {label}', f'admin:name:{p["user_id"]}'),
                            ('🚫 Отключить', f'admin:remove:{p["user_id"]}:{p["revision"]}')])
        for p in pending:
            text += f'\n@{p["username"]}: ' + ('ожидает /start' if p['state'] == 'waiting' else 'ожидает подтверждения')
            if p['candidate_id']:
                buttons.append([(f'Подтвердить @{p["username"]}', f'admin:approve:{p["id"]}:{p["candidate_id"]}')])
            buttons.append([(f'Отменить @{p["username"]}', f'admin:cancel:{p["id"]}')])
        if page:
            buttons.append([('Предыдущая страница', f'admin:list:{page - 1}')])
        if more:
            buttons.append([('Следующая страница', f'admin:list:{page + 1}')])
        buttons += [[('⚙️ Админское меню', 'admin:home')], [('🏠 Главное меню', 'menu:home')]]
        await reply(text, buttons)

    async def add(self, c, actor, value, reply):
        value = value.strip()
        if value.isascii() and value.isdecimal():
            uid = int(value)
            if uid <= 0 or uid > 2**52 - 1:
                raise InputError('Введите положительный Telegram ID или @username.')
            added = await self.grant(c, uid, actor)
            if not added:
                await reply('Этот пользователь уже имеет доступ.', [[('👥 Промоутеры', 'admin:list')]])
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
        if value.isascii() and value.isdecimal() and added:
            await self.input_session(c, actor, 'name_promoter', uid)
            await reply('Промоутер добавлен. Введите имя для списка:', [[('↩️ Позже', 'admin:list')]])

    async def handle(self, c, uid, text, action, reply):
        if await self.management(c, uid, text, action, reply):
            return True
        session = await one(c, 'SELECT * FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
        requested = action.startswith('admin:') or text.split(' ', 1)[0] in ('/promoters', '/add_promoter')
        if not await self.is_admin(c, uid):
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
        if requested:
            await c.execute('DELETE FROM admin_sessions WHERE bot_id=? AND branch_key=? AND user_id=?', (self.bot, self.branch, uid))
        try:
            if session and not requested:
                await self.add(c, uid, text, reply)
            elif text.startswith('/add_promoter '):
                await self.add(c, uid, text.split(' ', 1)[1], reply)
            elif action == 'admin:add' or text == '/add_promoter':
                await c.execute('INSERT OR REPLACE INTO admin_sessions(bot_id,branch_key,user_id,step,updated_at) VALUES(?,?,?,?,?)', (self.bot, self.branch, uid, 'add', now()))
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
                    await self.input_session(c, uid, 'name_promoter', int(candidate))
                    await reply('✅ Доступ промоутера подтверждён. Введите имя для списка:', [[('↩️ Позже', 'admin:list')]])
            elif action.startswith('admin:cancel:'):
                ident = action.split(':')[2]
                await c.execute("UPDATE promoter_invites SET state='cancelled' WHERE id=? AND bot_id=? AND branch_key=? AND state IN ('waiting','approval')", (ident, self.bot, self.branch))
                await self.list(c, reply)
            elif action.startswith(('admin:remove:', 'admin:revoke:')):
                _, command, target, revision = action.split(':')
                member = await one(c, '''SELECT p.*,u.full_name,u.username FROM promoters p LEFT JOIN telegram_users u ON u.bot_id=p.bot_id AND u.user_id=p.user_id
                    WHERE p.bot_id=? AND p.branch_key=? AND p.user_id=? AND p.revision=? AND p.active=1''', (self.bot, self.branch, int(target), int(revision)))
                if not member or await self.is_admin(c, int(target)):
                    await reply('Эта кнопка устарела или пользователь — администратор.')
                elif command == 'remove':
                    await reply(f'Отключить доступ промоутера «{self.label(member)}»? Сохранённые заявки останутся.', [[('Отключить', f'admin:revoke:{target}:{revision}')], [('Отмена', 'admin:list')]])
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
