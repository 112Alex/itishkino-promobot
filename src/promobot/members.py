"""Persistent numeric-ID grants and explicit approval of username candidates."""
import uuid
import json
from datetime import datetime, timedelta, timezone
from .domain import InputError, now, username, name, clean
from .storage import enqueue, one, rows

ADMIN_HELP = ('⚙️ Управление одним сообщением:\n'
    '/add_promoter 123456789 Максим — добавить промоутера\n'
    '/add_promoter @username Максим — приглашение по username\n'
    '/add_admin 123456789 Олег — добавить администратора\n'
    '/rename_promoter 123456789 Максим Иванов — изменить имя\n'
    '/rename_admin 123456789 Олег Иванов — изменить имя\n'
    '/remove_promoter 123456789 — отключить промоутера\n'
    '/promoters — список промоутеров\n/admins — список администраторов\n'
    '/watch Преображенка, Кузьминки — выбрать уведомления\n'
    '/watch все — все филиалы; /watch нет — отключить\n'
    '/watch — текущие подписки\n'
    'Лиды добавляйте обычным сообщением, как промоутеры. Числа в примерах — Telegram ID человека.')


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
        return (f'Аккаунт @{invite["username"]} связался с ботом. Telegram ID: {invite["candidate_id"]}.\n'
                f'Для подтверждения отправьте /approve_promoter {invite["id"]} {invite["candidate_id"]}\n'
                f'Для отмены: /cancel_invite {invite["id"]}', None)

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

    async def text_command(self, c, actor, text, reply):
        parts = text.strip().split(maxsplit=1)
        command = parts[0].split('@', 1)[0] if parts else ''
        commands = {'/admin', '/admins', '/promoters', '/add_promoter', '/add_admin',
                    '/rename_promoter', '/rename_admin', '/remove_promoter', '/watch',
                    '/approve_promoter', '/cancel_invite'}
        if command not in commands:
            return False
        if not await self.is_admin(c, actor):
            await reply('Только для администратора.')
            return True
        await c.execute('DELETE FROM admin_sessions WHERE bot_id=? AND user_id=?', (self.bot, actor))
        argument = parts[1].strip() if len(parts) > 1 else ''

        def numeric(value):
            if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= 2**52 - 1:
                raise InputError('Нужен положительный числовой Telegram ID.')
            return int(value)

        try:
            if command == '/admin':
                await reply(ADMIN_HELP)
            elif command in ('/promoters', '/admins'):
                page = int(argument or '1')
                if not 1 <= page <= 100000:
                    raise InputError('Номер страницы должен быть положительным.')
                table = 'promoters' if command == '/promoters' else 'administrators'
                predicate = ' AND p.branch_key=?' if table == 'promoters' else ''
                params = (self.bot, self.branch, (page-1)*20) if predicate else (self.bot, (page-1)*20)
                found = await rows(c, f'''SELECT p.*,u.full_name,u.username FROM {table} p LEFT JOIN telegram_users u
                    ON p.bot_id=u.bot_id AND p.user_id=u.user_id WHERE p.bot_id=?{predicate} AND p.active=1
                    ORDER BY p.user_id LIMIT 21 OFFSET ?''', params)
                value = ('👥 Промоутеры' if table == 'promoters' else '🛡 Администраторы') + f' — страница {page}:\n'
                value += '\n'.join(self.label(member, (page-1)*20+i+1).replace('Промоутер №', 'Администратор №') if table == 'administrators'
                    else self.label(member, (page-1)*20+i+1) for i, member in enumerate(found[:20])) or 'Пока нет.'
                if len(found) > 20:
                    value += f'\nСледующая страница: {command} {page+1}'
                if table == 'promoters' and page == 1:
                    pending = await rows(c, "SELECT * FROM promoter_invites WHERE bot_id=? AND branch_key=? AND state IN ('waiting','approval') ORDER BY created_at LIMIT 20", (self.bot, self.branch))
                    for invite in pending:
                        value += f'\n@{invite["username"]}: ' + ('ожидает /start' if invite['state'] == 'waiting' else 'ожидает подтверждения')
                        if invite['candidate_id']:
                            value += f'\n/approve_promoter {invite["id"]} {invite["candidate_id"]}'
                        value += f'\n/cancel_invite {invite["id"]}'
                await reply(value)
            elif command == '/watch':
                if argument:
                    if argument.casefold() in ('все', 'all'):
                        selected = list(self.s.all_branches)
                    elif argument.casefold() in ('нет', 'none'):
                        selected = []
                    else:
                        lookup = {v.casefold(): key for key, branch in self.s.all_branches.items()
                                  for v in (key, branch['name'], branch['name'].split('—')[-1].strip())}
                        requested = [value.strip().casefold() for value in argument.split(',')]
                        if any(value not in lookup for value in requested):
                            raise InputError('Филиал не найден. Доступные: ' + ', '.join(b['name'] for b in self.s.all_branches.values()))
                        selected = sorted({lookup[value] for value in requested})
                    await c.execute('INSERT OR REPLACE INTO admin_watches VALUES(?,?,?)', (self.bot, actor, json.dumps(selected)))
                old = await one(c, 'SELECT branches FROM admin_watches WHERE bot_id=? AND user_id=?', (self.bot, actor))
                selected = json.loads(old['branches']) if old else list(self.s.all_branches)
                await reply('🔔 Уведомления по заявкам: ' + (', '.join(self.s.all_branches[k]['name'] for k in selected if k in self.s.all_branches) or 'отключены') +
                    '\nОбщие сбои сервиса получают все администраторы.\nИзменить: /watch Преображенка, Кузьминки')
            elif command == '/cancel_invite':
                changed = await c.execute("UPDATE promoter_invites SET state='cancelled' WHERE id=? AND bot_id=? AND branch_key=? AND state IN ('waiting','approval')", (argument, self.bot, self.branch))
                await reply('Приглашение отменено.' if changed.rowcount else 'Активное приглашение не найдено.')
            elif command == '/approve_promoter':
                values = argument.split()
                if len(values) != 2:
                    raise InputError('Формат: /approve_promoter номер_приглашения Telegram_ID')
                ident, target = values[0], numeric(values[1])
                invite = await one(c, "SELECT * FROM promoter_invites WHERE id=? AND bot_id=? AND branch_key=? AND candidate_id=? AND state='approval'", (ident, self.bot, self.branch, target))
                known = await one(c, 'SELECT user_id FROM telegram_users WHERE bot_id=? AND user_id=? AND username=?', (self.bot, target, invite['username'])) if invite else None
                if not known:
                    raise InputError('Приглашение устарело или username изменился. Проверьте /promoters.')
                await self.grant(c, target, actor)
                if invite['display_name']:
                    await c.execute('UPDATE promoters SET display_name=? WHERE bot_id=? AND branch_key=? AND user_id=?', (invite['display_name'], self.bot, self.branch, target))
                await c.execute("UPDATE promoter_invites SET state='approved' WHERE id=?", (ident,))
                await reply('✅ Доступ промоутера подтверждён.')
            else:
                values = argument.split(maxsplit=1)
                if not values or (command != '/remove_promoter' and len(values) < 2):
                    raise InputError('Укажите ID и имя одним сообщением. Например: /add_promoter 123456789 Максим')
                if command == '/remove_promoter' and len(values) != 1:
                    raise InputError('Формат: /remove_promoter Telegram_ID')
                label = name(values[1], 70) if len(values) > 1 else None
                if command == '/add_promoter' and not (values[0].isascii() and values[0].isdecimal()):
                    handle = username(values[0])[1:]
                    invite = await one(c, "SELECT * FROM promoter_invites WHERE bot_id=? AND branch_key=? AND username=? AND state IN ('waiting','approval')", (self.bot, self.branch, handle))
                    if invite:
                        await c.execute('UPDATE promoter_invites SET display_name=? WHERE id=?', (label, invite['id']))
                    else:
                        ident = uuid.uuid4().hex[:16]
                        await c.execute('INSERT INTO promoter_invites(id,bot_id,branch_key,username,created_by,created_at,display_name) VALUES(?,?,?,?,?,?,?)', (ident, self.bot, self.branch, handle, actor, now(), label))
                        invite = await one(c, 'SELECT * FROM promoter_invites WHERE id=?', (ident,))
                    known = await one(c, 'SELECT user_id FROM telegram_users WHERE bot_id=? AND username=?', (self.bot, handle))
                    if known:
                        invite['candidate_id'] = known['user_id']
                        await c.execute("UPDATE promoter_invites SET state='approval',candidate_id=? WHERE id=?", (known['user_id'], invite['id']))
                        await reply(*self.confirmation(invite))
                    else:
                        await reply(f'Приглашение для «{label}» (@{handle}) сохранено. Пусть человек отправит /start; затем подтвердите его аккаунт.')
                else:
                    target = numeric(values[0])
                    if command == '/add_promoter':
                        if await self.is_admin(c, target):
                            raise InputError('У этого пользователя уже есть доступ администратора.')
                        await self.grant(c, target, actor)
                        await c.execute('UPDATE promoters SET display_name=?,updated_at=? WHERE bot_id=? AND branch_key=? AND user_id=?', (label, now(), self.bot, self.branch, target))
                        await reply(f'✅ Промоутер «{label}» добавлен.')
                    elif command == '/add_admin':
                        await c.execute('''INSERT INTO administrators(bot_id,user_id,display_name,added_by,updated_at) VALUES(?,?,?,?,?)
                            ON CONFLICT(bot_id,user_id) DO UPDATE SET active=1,display_name=excluded.display_name,revision=revision+1,updated_at=excluded.updated_at''', (self.bot, target, label, actor, now()))
                        await self.audit(c, actor, target, 'grant_admin')
                        await reply(f'✅ Администратор «{label}» добавлен. Он может добавлять других администраторов.')
                    elif command == '/remove_promoter':
                        if await self.is_admin(c, target):
                            raise InputError('Эта команда не отключает администраторов.')
                        changed = await c.execute('UPDATE promoters SET active=0,revision=revision+1,updated_at=? WHERE bot_id=? AND branch_key=? AND user_id=? AND active=1', (now(), self.bot, self.branch, target))
                        if changed.rowcount:
                            await self.audit(c, actor, target, 'revoke')
                        await reply('Промоутер отключён. Сохранённые заявки остаются.' if changed.rowcount else 'Активный промоутер не найден.')
                    else:
                        table = 'promoters' if command == '/rename_promoter' else 'administrators'
                        predicate = ' AND branch_key=?' if table == 'promoters' else ''
                        params = (label, now(), self.bot, target, self.branch) if predicate else (label, now(), self.bot, target)
                        changed = await c.execute(f'UPDATE {table} SET display_name=?,updated_at=? WHERE bot_id=? AND user_id=?{predicate} AND active=1', params)
                        if not changed.rowcount:
                            raise InputError('Пользователь с этой ролью не найден.')
                        await self.audit(c, actor, target, 'rename')
                        await reply(f'✅ Имя изменено: {label}.')
        except (InputError, ValueError) as exc:
            await reply('⚠️ ' + (str(exc) if isinstance(exc, InputError) else 'Проверьте аргументы команды.'))
        return True

    async def handle(self, c, uid, text, action, reply):
        if await self.text_command(c, uid, text, reply):
            return True
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
