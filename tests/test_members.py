import json
import pytest
from promobot.dialog import Dialog
from promobot.storage import Store
from promobot.telegram import Runtime


async def latest(app):
    r = (await app.db.query("SELECT payload FROM outbox WHERE kind IN ('send','ui') ORDER BY id DESC LIMIT 1"))[0]
    return json.loads(r['payload'])


@pytest.mark.parametrize('admin', [10001,10005,10006])
async def test_each_admin_can_grant_numeric_id_and_revoke(app, admin):
    app.s.administrators = {10005:'preobrazhenka',10006:'preobrazhenka'}
    app.s.promoters.clear()
    await app.event('/start',uid=admin)
    assert any(a == 'admin:home' for row in (await latest(app))['keyboard'] for _,a in row)
    await app.event(callback='admin:add',uid=admin)
    await app.event('20001',uid=admin)
    await app.event('/new',uid=20001)
    assert (await app.draft(20001))['step']=='parent'
    p=(await app.db.query('SELECT * FROM promoters WHERE user_id=20001'))[0]
    await app.event(callback=f'admin:remove:20001:{p["revision"]}',uid=admin)
    assert 'Отключить доступ' in (await latest(app))['text']
    await app.event(callback=f'admin:revoke:20001:{p["revision"]}',uid=admin)
    await app.event('/new',uid=20001)
    assert 'Доступ не разрешён' in (await latest(app))['text']
    # Old revoke button cannot remove a re-added user.
    await app.event('/add_promoter 20001',uid=admin)
    await app.event(callback=f'admin:revoke:20001:{p["revision"]}',uid=admin)
    assert (await app.db.query('SELECT active FROM promoters WHERE user_id=20001'))[0]['active']==1


async def test_username_requires_start_and_admin_numeric_confirmation(app):
    app.s.promoters.clear()
    await app.event('/add_promoter @new_person',uid=app.s.admin)
    await app.event('/start',uid=20001,username='NEW_PERSON')
    assert 'Дождитесь подтверждения' in (await latest(app))['text']
    invite=(await app.db.query('SELECT * FROM promoter_invites'))[0]
    assert invite['state']=='approval' and invite['candidate_id']==20001
    # Promoter cannot approve themselves.
    await app.event(callback=f'admin:approve:{invite["id"]}:20001',uid=20001,username='new_person')
    assert not await app.db.query('SELECT * FROM promoters')
    await app.event(callback=f'admin:approve:{invite["id"]}:20001',uid=app.s.admin)
    await app.event('/new',uid=20001,username='changed_name')
    assert (await app.draft(20001))['step']=='parent'
    # Taking the old username never transfers an existing numeric grant.
    await app.event('/new',uid=20002,username='new_person')
    assert not await app.draft(20002)
    assert 'Доступ не разрешён' in (await latest(app))['text']


async def test_known_username_reassignment_invalidates_old_confirmation(app):
    app.s.promoters.clear()
    await app.event('/id',uid=20001,username='known_person')
    await app.event('/add_promoter known_person',uid=app.s.admin)
    invite=(await app.db.query('SELECT * FROM promoter_invites'))[0]
    await app.event('/id',uid=20002,username='known_person')
    await app.event(callback=f'admin:approve:{invite["id"]}:20001',uid=app.s.admin)
    assert not await app.db.query('SELECT * FROM promoters')
    await app.event(callback=f'admin:approve:{invite["id"]}:20002',uid=app.s.admin)
    assert (await app.db.query('SELECT user_id FROM promoters'))[0]['user_id']==20002


async def test_invite_cancel_and_cross_branch_buttons(app):
    app.s.promoters.clear()
    await app.event('/add_promoter @future_person',uid=app.s.admin)
    invite=(await app.db.query('SELECT * FROM promoter_invites'))[0]
    await app.event(callback=f'admin:cancel:{invite["id"]}',uid=app.s.admin)
    await app.event('/start',uid=20001,username='future_person')
    assert not await app.db.query('SELECT * FROM promoters')
    await app.event(callback=f'admin:approve:{invite["id"]}:20001',uid=app.s.admin)
    assert not await app.db.query('SELECT * FROM promoters')


async def test_members_persist_and_revocation_not_overwritten_by_legacy_config(app):
    await app.event('/promoters',uid=app.s.admin)
    p=(await app.db.query('SELECT * FROM promoters WHERE user_id=10002'))[0]
    await app.event(callback=f'admin:revoke:10002:{p["revision"]}',uid=app.s.admin)
    await app.db.close()
    db=await Store(app.s.database).open()
    app.db.conn,app.db.lock=db.conn,db.lock
    app.dialog=Dialog(app.db,app.s,'42')
    # event fixture references original dialog, whose DB is now re-opened.
    await app.event('/new',uid=10002)
    assert 'Доступ не разрешён' in (await latest(app))['text']
    assert (await app.db.query('SELECT active FROM promoters WHERE user_id=10002'))[0]['active']==0


async def test_admin_add_input_does_not_overwrite_family_draft(app):
    await app.event('/new',uid=app.s.admin)
    before=await app.draft(app.s.admin)
    await app.event(callback='admin:add',uid=app.s.admin)
    await app.event('not-a-username',uid=app.s.admin)
    assert (await app.draft(app.s.admin))['data']==before['data']
    await app.event(callback='admin:list',uid=app.s.admin)
    await app.event('Родитель',uid=app.s.admin)
    assert (await app.draft(app.s.admin))['data']['parent']=='Родитель'


async def test_network_errors_reach_three_admins_once_per_transition(app):
    app.s.administrators={10005:'preobrazhenka',10006:'preobrazhenka'}
    runtime=Runtime(app.db,app.s,None,app.dialog,app.worker)
    await runtime.network_state('down')
    await runtime.network_state('down')
    found=await app.db.query('SELECT chat_id FROM outbox')
    assert sorted(x['chat_id'] for x in found)==[10001,10005,10006]


async def test_non_admin_commands_and_group_do_not_grant(app):
    await app.event('/add_promoter 20001',uid=10002)
    await app.event('/add_promoter 20001',uid=app.s.admin,chat_type='group')
    assert not await app.db.query('SELECT * FROM promoters WHERE user_id=20001')


async def test_duplicate_grant_no_extra_audit_and_invalid_id(app):
    await app.event('/add_promoter 20001',uid=app.s.admin)
    await app.event('/add_promoter 20001',uid=app.s.admin)
    await app.event('/add_promoter 0',uid=app.s.admin)
    await app.event('/add_promoter 9999999999999999999',uid=app.s.admin)
    assert len(await app.db.query('SELECT * FROM promoters WHERE user_id=20001'))==1
    assert len(await app.db.query("SELECT * FROM membership_audit WHERE target_id=20001 AND action='grant'"))==1


async def test_promoters_list_pages_are_bounded(app):
    app.s.promoters.clear()
    async with app.db.tx() as c:
        for uid in range(20001,20042): await app.dialog.members.grant(c,uid,app.s.admin)
    await app.event('/promoters',uid=app.s.admin)
    p=await latest(app)
    assert '20001' not in p['text'] and '20041' not in p['text']
    assert any(a.startswith('admin:name:20001') for row in p['keyboard'] for _,a in row)
    assert not any('20041' in a for row in p['keyboard'] for _,a in row)
    assert sum(len(row) for row in p['keyboard'])<100
    assert any(a=='admin:list:1' for row in p['keyboard'] for _,a in row)
    await app.event(callback='admin:list:2',uid=app.s.admin)
    assert any(a == 'admin:name:20041' for row in (await latest(app))['keyboard'] for _,a in row)


async def test_foreign_branch_invite_cannot_be_approved(app):
    app.s.promoters.clear()
    await app.event('/add_promoter @foreign_person',uid=app.s.admin)
    invite=(await app.db.query('SELECT * FROM promoter_invites'))[0]
    await app.event('/start',uid=20001,username='foreign_person')
    await app.db.execute("UPDATE promoter_invites SET branch_key='other'")
    await app.event(callback=f'admin:approve:{invite["id"]}:20001',uid=app.s.admin)
    assert not await app.db.query('SELECT * FROM promoters')


async def test_admin_input_session_expires_without_changing_draft(app):
    await app.event(callback='admin:add',uid=app.s.admin)
    await app.db.execute("UPDATE admin_sessions SET updated_at='2020-01-01T00:00:00+00:00'")
    await app.event('20001',uid=app.s.admin)
    assert not await app.db.query('SELECT * FROM promoters WHERE user_id=20001')
    assert 'истекло' in (await latest(app))['text']
