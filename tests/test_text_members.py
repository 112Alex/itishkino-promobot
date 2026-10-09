import json
import pytest
from promobot.storage import Store
from test_multibranch import franchise, latest


@pytest.mark.parametrize('actor', [10001,10005,10006])
async def test_admin_adds_named_promoter_in_one_message_and_renames_revokes(app, actor):
    franchise(app)
    await app.event('/add_promoter 30001 Максим', uid=actor)
    p = (await app.db.query('SELECT * FROM promoters WHERE user_id=30001'))[0]
    assert p['active'] == 1 and p['display_name'] == 'Максим'
    assert not await app.db.query('SELECT * FROM admin_sessions')
    await app.event('/rename_promoter 30001 Максим Иванов', uid=actor)
    await app.event('/promoters', uid=actor)
    assert 'Максим Иванов' in (await latest(app))['text'] and '30001' not in (await latest(app))['text']
    assert not (await latest(app))['keyboard']
    await app.event('/remove_promoter 30001', uid=actor)
    await app.event('Мария Игорь 6 +79991234567', uid=30001)
    assert not await app.db.query('SELECT * FROM requests')


async def test_new_admin_can_add_other_admin_and_submit_lead(app):
    await app.event('/add_admin 20001 Сергей', uid=app.s.admin)
    await app.event('/add_admin 20002 Мария', uid=20001)
    await app.event('/add_promoter 30001 Максим', uid=20002)
    await app.event('/rename_admin 20002 Мария Иванова', uid=20001)
    await app.event('/admins', uid=20001)
    assert 'Мария Иванова' in (await latest(app))['text']
    await app.event('Мария Игорь 6 +79991234567', uid=20002)
    assert (await app.db.query('SELECT user_id FROM requests')) == [{'user_id':20002}]
    await app.db.close()
    reopened = await Store(app.s.database).open()
    app.db.conn, app.db.lock = reopened.conn, reopened.lock
    await app.event('/admin', uid=20002)
    assert '/add_admin' in (await latest(app))['text']


@pytest.mark.parametrize('command', ['/add_promoter 30001 Максим', '/add_admin 30001 Олег',
    '/rename_promoter 10003 Максим', '/rename_admin 10001 Сергей', '/remove_promoter 10003',
    '/watch нет', '/admins', '/admin', '/approve_promoter fake 30001', '/cancel_invite fake'])
async def test_nonadmin_cannot_use_management_commands(app, command):
    await app.event(command)
    assert 'Только для администратора' in (await latest(app))['text']
    assert not await app.db.query('SELECT * FROM membership_audit WHERE actor_id=10002')
    assert not await app.db.query('SELECT * FROM admin_watches')


@pytest.mark.parametrize('command', ['/add_promoter 0 Максим', '/add_admin -1 Олег',
    '/add_admin 999999999999999999999 Олег', '/add_promoter 30001', '/add_admin 30001',
    '/rename_promoter 12345678 Максим', '/promoters xyz', '/watch Неизвестный',
    '/approve_promoter fake nope', '/remove_promoter 10001'])
async def test_invalid_admin_command_never_partially_grants_role(app, command):
    await app.event(command, uid=app.s.admin)
    assert '⚠️' in (await latest(app))['text']
    assert not await app.db.query('SELECT * FROM promoters WHERE user_id=30001')
    assert not await app.db.query('SELECT * FROM administrators WHERE user_id=30001')
    assert not await app.db.query('SELECT * FROM admin_watches')


async def test_named_username_invite_requires_start_and_numeric_approval(app):
    app.s.promoters.clear()
    await app.event('/add_promoter @new_person Максим Иванов', uid=app.s.admin)
    await app.event('/start', uid=20001, username='new_person')
    invite = (await app.db.query('SELECT * FROM promoter_invites'))[0]
    assert invite['display_name'] == 'Максим Иванов' and invite['state'] == 'approval'
    notice = json.loads((await app.db.query("SELECT payload FROM outbox WHERE dedupe LIKE 'invite:%'"))[0]['payload'])
    assert not notice['keyboard'] and '/approve_promoter' in notice['text']
    await app.event(f'/approve_promoter {invite["id"]} 20001', uid=20001, username='new_person')
    assert not await app.db.query('SELECT * FROM promoters')
    await app.event(f'/approve_promoter {invite["id"]} 20001', uid=app.s.admin)
    p = (await app.db.query('SELECT * FROM promoters'))[0]
    assert p['user_id'] == 20001 and p['display_name'] == 'Максим Иванов'
    await app.event(f'/approve_promoter {invite["id"]} 20001', uid=app.s.admin)
    assert len(await app.db.query('SELECT * FROM promoters')) == 1


async def test_known_username_reassignment_and_cancel_cannot_grant_wrong_person(app):
    app.s.promoters.clear()
    await app.event('/id', uid=20001, username='known_person')
    await app.event('/add_promoter @known_person Максим', uid=app.s.admin)
    invite = (await app.db.query('SELECT * FROM promoter_invites'))[0]
    await app.event('/id', uid=20002, username='known_person')
    await app.event(f'/approve_promoter {invite["id"]} 20001', uid=app.s.admin)
    assert not await app.db.query('SELECT * FROM promoters')
    await app.event(f'/cancel_invite {invite["id"]}', uid=app.s.admin)
    await app.event(f'/approve_promoter {invite["id"]} 20002', uid=app.s.admin)
    assert not await app.db.query('SELECT * FROM promoters')


async def test_watch_text_accepts_names_keys_and_is_independent(app):
    franchise(app)
    await app.event('/watch Преображенка, Кузьминки', uid=10001)
    await app.event('/watch maryino', uid=10005)
    watches = {r['user_id']:json.loads(r['branches']) for r in await app.db.query('SELECT * FROM admin_watches')}
    assert set(watches[10001]) == {'preobrazhenka','kuzminki'} and watches[10005] == ['maryino']
    await app.event('/watch все', uid=10005)
    await app.event('/watch нет', uid=10001)
    assert json.loads((await app.db.query('SELECT branches FROM admin_watches WHERE user_id=10001'))[0]['branches']) == []
    await app.event('/watch', uid=10005)
    assert 'Марьино' in (await latest(app))['text'] and not (await latest(app))['keyboard']


async def test_management_preserves_lead_draft_and_does_not_swallow_next_family(app):
    await app.event('/new', uid=app.s.admin)
    before = (await app.draft(app.s.admin))['data']
    await app.event('/add_promoter 30001 Максим', uid=app.s.admin)
    assert (await app.draft(app.s.admin))['data'] == before
    await app.event('Мария Игорь 6 +79991234567', uid=app.s.admin)
    assert (await app.db.query('SELECT user_id FROM requests')) == [{'user_id': app.s.admin}]
