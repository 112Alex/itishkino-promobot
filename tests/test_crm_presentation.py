import html
import json
import pytest
from promobot.crm import CRMError
from promobot.domain import communication, crm_payload


@pytest.mark.parametrize('preference,short', [('Только писать','писать'),('Только звонить','звонить'),('Можно оба способа','писать/звонить')])
async def test_note_contains_only_moscow_minutes_and_contact_choice(app, preference, short):
    r = await app.intake()
    r['data'] = json.loads(r['data'])
    r['data']['preference'] = preference
    r['confirmed_at'] = '2026-10-06T15:04:59+00:00'
    payload = crm_payload(r, app.s)
    assert payload['note'] == '06.10.2026 18:04\n' + short
    assert payload[app.s.branch['request_field']] == r['id']
    for field, value in payload.items():
        if field != app.s.branch['request_field']:
            assert r['id'] not in str(value)
    assert r['id'] not in communication(r) and '[promobot:' not in communication(r)


async def test_identical_preexisting_comment_is_not_treated_as_our_write(app):
    r = await app.intake()
    r['data'] = json.loads(r['data'])
    expected = communication(r)
    create = app.crm.create
    add = app.crm.add_comment
    async def existing(payload):
        model = await create(payload)
        await add(model['id'], expected)
        return model
    app.crm.create = existing
    await app.worker.tick()
    saved = (await app.db.query('SELECT * FROM requests'))[0]
    assert saved['state'] == 'delivered' and saved['communication_id'] == 2
    assert json.loads(saved['data'])['_comment_before'] == [1]
    assert len(await app.crm.comments(saved['crm_id'])) == 2


async def test_plain_comment_baseline_survives_timeout_and_worker_restart(app):
    from promobot.worker import Worker
    await app.intake()
    add = app.crm.add_comment
    calls = 0
    async def lost(*args):
        nonlocal calls
        calls += 1
        saved = (await app.db.query('SELECT data FROM requests'))[0]
        assert json.loads(saved['data'])['_comment_before'] == []
        assert (await app.db.query('SELECT phase FROM jobs'))[0]['phase'] == 'reconcile_comment'
        await add(*args)
        raise CRMError('network', True, True)
    app.crm.add_comment = lost
    await app.worker.tick()
    await app.db.execute('UPDATE jobs SET next_at=0')
    worker = Worker(app.db, app.s, app.crm)
    await worker.recover()
    await worker.tick()
    saved = (await app.db.query('SELECT state,communication_id FROM requests'))[0]
    assert saved == {'state': 'delivered', 'communication_id': 1}
    assert calls == 1 and len(await app.crm.comments(1)) == 1


async def test_two_new_identical_comments_after_lost_response_require_review(app):
    await app.intake()
    add = app.crm.add_comment
    calls = 0
    async def ambiguous(*args):
        nonlocal calls
        calls += 1
        await add(*args)
        await add(*args)
        raise CRMError('network', True, True)
    app.crm.add_comment = ambiguous
    await app.worker.tick()
    await app.db.execute('UPDATE jobs SET next_at=0')
    await app.worker.tick()
    saved = (await app.db.query('SELECT state,error FROM requests'))[0]
    assert saved == {'state': 'manual_review', 'error': 'comment_not_unique'}
    assert calls == 1 and len(await app.crm.comments(1)) == 2


async def test_old_queued_payload_stays_stable_across_upgrade(app):
    r = await app.intake()
    r['data'] = json.loads(r['data'])
    del r['data']['crm_format']
    await app.db.execute('UPDATE requests SET data=? WHERE id=?', (json.dumps(r['data']), r['id']))
    old_payload = crm_payload(r, app.s)
    assert r['id'] in old_payload['note'] and r['id'] in communication(r)
    model = await app.crm.create(old_payload)
    await app.db.execute("UPDATE jobs SET phase='reconcile_create'")
    await app.worker.tick()
    assert (await app.db.query('SELECT state FROM requests'))[0]['state'] == 'delivered'
    assert (await app.crm.customers())[0] == model
    assert (await app.crm.comments(model['id']))[0]['comment'] == communication(r)
