import json
import pytest
from promobot.config import ConfigurationError


async def reply(app):
    row = (await app.db.query("SELECT payload FROM outbox WHERE kind='send' ORDER BY id DESC LIMIT 1"))[0]
    return json.loads(row['payload'])


async def test_branch_admin_can_read_and_retry_but_not_override_duplicate(app):
    app.s.administrators = {10005: 'preobrazhenka'}
    assert app.s.authorized(10005) and app.s.is_admin(10005)
    r = await app.intake()
    await app.event('/start', uid=10005)
    buttons = (await reply(app))['keyboard']
    assert any(a == 'menu:problems' for row in buttons for _, a in row)
    await app.event('/request ' + r['id'], uid=10005)
    assert 'Анна' in (await reply(app))['text']
    await app.db.execute("UPDATE requests SET state='manual_review'")
    await app.db.execute("UPDATE jobs SET state='held',phase='reconcile_create'")
    await app.event('/retry ' + r['id'], uid=10005)
    j = (await app.db.query('SELECT * FROM jobs'))[0]
    assert j['state'] == 'pending' and j['phase'] == 'reconcile_create'
    await app.db.execute("UPDATE requests SET state='duplicate_review'")
    await app.db.execute("UPDATE jobs SET state='done'")
    await app.event('/retry ' + r['id'], uid=10005)
    assert 'запрещает' in (await reply(app))['text']
    assert (await app.db.query('SELECT state FROM jobs'))[0]['state'] == 'done'


async def test_admin_notifications_go_to_all_admins(app):
    app.s.administrators = {10005: 'preobrazhenka'}
    await app.intake()
    await app.db.execute("DELETE FROM outbox")
    await app.crm.create({'name': 'Old', 'branch_ids': [901], 'phone': ['+79991234567'], 'web': []})
    await app.worker.tick()
    chats = {r['chat_id'] for r in await app.db.query('SELECT chat_id FROM outbox')}
    assert chats == {app.s.admin, 10002, 10005}


async def test_admin_cannot_edit_someone_elses_draft(app):
    app.s.administrators = {10005: 'preobrazhenka'}
    await app.intake(confirm=False)
    d = await app.draft()
    await app.event(callback=f'd:{d["id"]}:{d["version"]}:confirm', uid=10005)
    assert not await app.db.query('SELECT * FROM requests')
    assert (await app.draft())['step'] == 'review'


def test_admin_wrong_branch_rejected():
    from pathlib import Path
    from promobot.config import Settings
    cfg = json.loads(Path('config/mock.json').read_text())
    s = Settings('test', 'mock', Path('/tmp/test.sqlite3'), cfg['branch'], {}, 1,
                 administrators={2: 'other_branch'})
    with pytest.raises(ConfigurationError):
        s.validate()
