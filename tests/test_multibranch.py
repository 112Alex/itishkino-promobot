import asyncio
import json
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import EditMessageText
from promobot.access import administrators
from promobot.config import ConfigurationError, Settings
from promobot.crm import CRMError
from promobot.storage import Store
from promobot.telegram import Outbox, Runtime, keyboard


def franchise(app):
    app.s.branches = {app.s.branch['key']: app.s.branch,
                      'kuzminki': {**app.s.branch, 'key': 'kuzminki', 'name': 'Кузьминки', 'crm_id': 905},
                      'maryino': {**app.s.branch, 'key': 'maryino', 'name': 'Марьино', 'crm_id': 906},
                      'zhulebino': {**app.s.branch, 'key': 'zhulebino', 'name': 'Жулебино', 'crm_id': 907}}
    app.s.administrators = {10005: 'preobrazhenka', 10006: 'preobrazhenka'}


async def latest(app, uid=None):
    sql = "SELECT payload FROM outbox WHERE kind='ui'"
    args = ()
    if uid:
        sql += ' AND chat_id=?'
        args = (uid,)
    return json.loads((await app.db.query(sql + ' ORDER BY id DESC LIMIT 1', args))[0]['payload'])


async def watch_only(app, admin, branch):
    await app.event(callback='admin:watch', uid=admin)
    for key in app.s.all_branches:
        if key != branch:
            await app.event(callback='admin:watch:' + key, uid=admin)


async def test_two_promoters_same_contact_separate_branches_and_comments(app):
    franchise(app)
    first, second = await asyncio.gather(
        app.intake(uid=10002, branch='preobrazhenka', parent='Анна', comment='Семья один'),
        app.intake(uid=10003, branch='kuzminki', parent='Ирина', comment='Семья два'))
    assert first['branch_key'] == 'preobrazhenka' and second['branch_key'] == 'kuzminki'
    assert json.loads(first['data'])['crm_settings']['crm_id'] == 901
    assert json.loads(second['data'])['crm_settings']['crm_id'] == 905
    await asyncio.gather(app.worker.tick(), app.worker.tick())
    requests = await app.db.query('SELECT state,crm_id,user_id FROM requests')
    assert len(requests) == 2 and all(r['state'] == 'delivered' for r in requests)
    assert len({r['crm_id'] for r in requests}) == 2
    for r in (first, second):
        crm = app.crm.for_branch(r['branch_key'])
        records = await crm.customers()
        assert len(records) == 1 and records[0]['branch_ids'] == [r['crm_branch_id']]
        assert records[0]['legal_name'] == json.loads(r['data'])['parent']
        comments = await crm.comments(records[0]['id'])
        assert len(comments) == 1
        assert comments[0]['branch_id'] == r['crm_branch_id']
        assert json.loads(r['data'])['comment'] in comments[0]['comment']
        assert r['id'] in comments[0]['comment']


async def test_branch_preference_cannot_move_existing_draft_and_explicit_edit_can(app):
    franchise(app)
    await app.intake(branch='preobrazhenka', confirm=False)
    original = await app.draft()
    await app.event(callback='branch:select:kuzminki')
    assert (await app.draft())['branch_key'] == 'preobrazhenka'
    await app.event(action='edit')
    await app.event(action='ebranch')
    await app.event(action='branch.maryino')
    d = await app.draft()
    assert d['branch_key'] == 'maryino' and d['crm_branch_id'] == 906 and d['step'] == 'review'
    assert d['data']['parent'] == original['data']['parent']
    assert d['data']['children'] == original['data']['children']
    await app.event(action='confirm')
    await app.worker.tick()
    assert (await app.crm.for_branch('maryino').customers())[0]['branch_ids'] == [906]
    assert not await app.crm.customers() and not await app.crm.for_branch('kuzminki').customers()
    await app.event(action='menu:new')
    assert (await app.draft())['branch_key'] == 'kuzminki'


async def test_branch_back_restores_branch_and_old_foreign_button_is_rejected(app):
    franchise(app)
    await app.event(action='menu:new')
    old = await app.draft()
    await app.event(action='branch.kuzminki')
    await app.event(action='back')
    assert (await app.draft())['branch_key'] == 'preobrazhenka'
    assert (await app.draft())['step'] == 'branch'
    await app.event(callback=f'd:{old["id"]}:{old["version"]}:branch.zhulebino')
    assert 'устарела' in (await latest(app))['text']
    await app.event(action='menu:new', uid=10003)
    await app.event(callback=f'd:{old["id"]}:{old["version"]}:branch.zhulebino', uid=10003)
    assert (await app.draft(10003))['branch_key'] == 'preobrazhenka'


async def test_branch_failure_notices_follow_each_admin_subscriptions(app):
    franchise(app)
    await watch_only(app, 10001, 'preobrazhenka')
    await watch_only(app, 10005, 'kuzminki')
    await watch_only(app, 10006, 'maryino')
    first = await app.intake(branch='preobrazhenka')
    second = await app.intake(uid=10003, branch='kuzminki')
    await app.db.execute('DELETE FROM outbox')
    # Changing each saved CRM contract forces a safe failure before any creation.
    app.s.branch['source_id'] = 990
    app.s.branches['kuzminki']['source_id'] = 991
    await asyncio.gather(app.worker.tick(), app.worker.tick())
    notices = await app.db.query('SELECT chat_id,request_id FROM outbox')
    assert {(r['chat_id'], r['request_id']) for r in notices} == {(10001, first['id']), (10005, second['id'])}
    assert not await app.crm.customers() and not await app.crm.for_branch('kuzminki').customers()
    # A subscription controls notifications, not access to another branch's request.
    await app.event('/request ' + second['id'], uid=10001)
    assert 'Кузьминки' in (await latest(app))['text']


async def test_dynamic_admin_can_add_admin_and_named_promoter_and_roles_persist(app):
    franchise(app)
    await app.event(callback='admin:addadmin', uid=10001)
    await app.event('20001', uid=10001)
    await app.event('Сергей', uid=10001)
    await app.event(callback='admin:addadmin', uid=20001)
    await app.event('20002', uid=20001)
    await app.event('Мария', uid=20001)
    await app.event('/add_promoter 30001', uid=20002)
    await app.event('Максим', uid=20002)
    await app.event('/promoters', uid=20001)
    data = await latest(app)
    assert 'Максим' in data['text'] and '30001' not in data['text']
    assert all('30001' not in label for row in data['keyboard'] for label, _ in row)
    await app.event(callback='admin:name:30001', uid=20001)
    await app.event('Максим Робототехника', uid=20001)
    await app.event('/start', uid=30001, username='max_new_name')
    await app.event('/promoters', uid=20001)
    assert 'Максим Робототехника' in (await latest(app))['text']
    # Reopening and seeding legacy ENV must retain new admins and edited names.
    await app.db.close()
    reopened = await Store(app.s.database).open()
    app.db.conn, app.db.lock = reopened.conn, reopened.lock
    await app.event('/start', uid=20002)
    assert any(a == 'admin:home' for row in (await latest(app))['keyboard'] for _, a in row)
    await app.event('/new', uid=30001)
    assert (await app.draft(30001))['step'] == 'branch'


async def test_nonadmin_cannot_forge_roles_names_or_watch_preferences(app):
    franchise(app)
    for action in ('admin:addadmin', 'admin:name:10003', 'admin:aname:10001', 'admin:watch:kuzminki'):
        await app.event(callback=action, uid=10002)
        assert 'Только для администратора' in (await latest(app))['text']
    assert not await app.db.query('SELECT * FROM admin_watches')
    assert not await app.db.query('SELECT * FROM membership_audit WHERE action IN (\'grant_admin\',\'rename\')')


async def test_admin_watch_marks_persist_and_global_outage_reaches_every_admin(app):
    franchise(app)
    await watch_only(app, 10005, 'kuzminki')
    await app.event(callback='admin:watch', uid=10005)
    p = await latest(app)
    assert [label for row in p['keyboard'] for label, action in row if action == 'admin:watch:kuzminki'][0].startswith('✅')
    assert [label for row in p['keyboard'] for label, action in row if action == 'admin:watch:maryino'][0].startswith('⬜')
    async with app.db.tx() as c:
        assert await administrators(c, app.s, 'kuzminki') == [10001, 10005, 10006]
        assert await administrators(c, app.s, 'maryino') == [10001, 10006]
    await app.db.execute('DELETE FROM outbox')
    await Runtime(app.db, app.s, None, app.dialog, app.worker).network_state('down')
    assert {r['chat_id'] for r in await app.db.query('SELECT chat_id FROM outbox')} == {10001, 10005, 10006}


class UIBot:
    def __init__(self):
        self.sent, self.edited, self.acks = [], [], []
        self.error = None

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text, kw))
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def edit_message_text(self, text, **kw):
        if self.error:
            error, self.error = self.error, None
            raise error
        self.edited.append((text, kw))
        return True

    async def answer_callback_query(self, text):
        self.acks.append(text)


async def drain(outbox):
    for _ in range(200):
        if not await outbox.tick():
            return
    raise AssertionError('outbox did not drain')


async def test_navigation_and_text_answers_edit_one_message_per_user_after_restart(app):
    franchise(app)
    bot = UIBot()
    out = Outbox(app.db, bot, app.s)
    await app.event('/start')
    await drain(out)
    await app.event(action='menu:new')
    await app.event(action='branch.kuzminki')
    await app.event('Анна')
    await drain(out)
    assert len(bot.sent) == 1 and len(bot.edited) == 3
    assert {kw['message_id'] for _, kw in bot.edited} == {101}
    # Pointer lives in SQLite, survives creating a new outbox service.
    out = Outbox(app.db, bot, app.s)
    await app.event(action='phone')
    await app.event('+79991234567')
    await drain(out)
    assert len(bot.sent) == 1 and len(bot.edited) == 5
    assert any(b.text.startswith('✅ Телефон') for row in bot.edited[-1][1]['reply_markup'].inline_keyboard for b in row)
    for _, kw in bot.edited:
        for row in kw['reply_markup'].inline_keyboard:
            for button in row:
                assert ord(button.text[0]) > 0x2000 and len(button.callback_data.encode()) <= 64
    await app.event('/start', uid=10003)
    await drain(out)
    assert len(bot.sent) == 2 and bot.sent[-1][0] == 10003


@pytest.mark.parametrize('message,creates_new', [('message is not modified', False), ('message to edit not found', True), ("message can't be edited", True)])
async def test_ui_handles_unchanged_and_deleted_menu_without_spam(app, message, creates_new):
    bot = UIBot()
    out = Outbox(app.db, bot, app.s)
    await app.event('/start')
    await drain(out)
    bot.error = TelegramBadRequest(method=EditMessageText(text='test'), message=message)
    await app.event('/start')
    await drain(out)
    assert len(bot.sent) == 1 + creates_new
    assert not await app.db.query("SELECT * FROM outbox WHERE state IN ('pending','held')")
    assert (await app.db.query('SELECT message_id FROM ui_messages'))[0]['message_id'] == 101 + creates_new


async def test_ui_rate_limit_keeps_same_pointer_and_callback_ack_is_not_blocked(app):
    bot = UIBot()
    out = Outbox(app.db, bot, app.s)
    await app.event('/start')
    await drain(out)
    bot.error = TelegramRetryAfter(method=EditMessageText(text='test'), message='rate limit', retry_after=10)
    await app.event('/start')
    assert await out.tick()
    await app.event(action='menu:new')
    # Ack must be serviced even while the previous UI update waits to retry.
    for _ in range(4):
        if not await out.tick():
            break
    assert bot.acks and len([m for m in bot.sent if m[0] == 10002]) == 1
    await app.db.execute("UPDATE outbox SET next_at=0 WHERE kind='ui'")
    await drain(out)
    assert len(bot.sent) == 2  # One separately queued administrator error notice.
    assert bot.edited[-1][1]['message_id'] == 101


def test_csv_initial_admins_without_owner_and_branch_settings_are_immutable(tmp_path, monkeypatch):
    config = json.loads(open('config/mock.json').read())
    config['branches'] = [config['branch'], {**config['branch'], 'key': 'kuzminki', 'name': 'Кузьминки', 'crm_id': 905}]
    p = tmp_path / 'config.json'
    p.write_text(json.dumps(config))
    monkeypatch.setenv('CONFIG_PATH', str(p))
    monkeypatch.setenv('ENVIRONMENT', 'test')
    monkeypatch.setenv('CRM_MODE', 'mock')
    monkeypatch.setenv('ADMIN_TELEGRAM_ID', '')
    monkeypatch.setenv('ADMIN_TELEGRAM_IDS', '101, 102,103')
    config['administrators'] = {'999': 'preobrazhenka'}
    p.write_text(json.dumps(config))
    s = Settings.load()
    assert s.admin == 101 and s.admin_ids == (101, 102, 103, 999)
    other = s.for_branch('kuzminki')
    assert other.branch['crm_id'] == 905 and s.branch['crm_id'] == 901
    config['branches'][1]['crm_id'] = 901
    p.write_text(json.dumps(config))
    with pytest.raises(ConfigurationError):
        Settings.load()


async def test_long_unicode_review_pages_edit_same_message_and_cannot_be_replayed(app):
    await app.intake(confirm=False)
    draft = await app.draft()
    data = draft['data']
    data['children'] = [{'name': 'А' + '🙂'*190, 'age': 9} for _ in range(12)]
    data['comment'] = '🙂'*1400
    data['title'] = 'Семья'
    await app.db.execute('UPDATE drafts SET data=? WHERE id=?', (json.dumps(data), draft['id']))
    await app.db.execute('DELETE FROM outbox')
    bot = UIBot()
    out = Outbox(app.db, bot, app.s)
    await app.event(action='menu:continue')
    await drain(out)
    screen = (await app.db.query('SELECT * FROM ui_screens'))[0]
    pages = json.loads(screen['pages'])
    assert len(pages) > 1 and all(len(p.encode('utf-16-le'))//2 <= 3500 for p in pages)
    assert ''.join(pages) == app.dialog.screen(await app.draft())[0]
    for page in range(1, len(pages)):
        await app.event(callback=f'page:{screen["token"]}:{page}')
        await drain(out)
        assert pages[page] in bot.edited[-1][0]
    assert len(bot.sent) == 1
    await app.event('/start')
    await app.event(callback=f'page:{screen["token"]}:1')
    assert 'устарела' in (await latest(app))['text']


async def test_multibranch_mock_acceptance_and_global_branch_dictionary(app, capsys):
    from promobot.cli import bootstrap
    from promobot.demo import acceptance
    franchise(app)
    await bootstrap(app.crm, app.s)
    report = json.loads(capsys.readouterr().out)
    assert len(report['dictionaries']['branch']) == 4
    app.s.token = '42:synthetic-only'
    await acceptance(app.db, app.s, app.crm)
    assert (await app.db.query('SELECT state FROM requests')) == [{'state': 'delivered'}]
