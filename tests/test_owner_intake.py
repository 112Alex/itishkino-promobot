import json


async def test_owner_can_add_and_deliver_lead_without_promoter_role(app):
    owner = app.s.admin
    assert owner not in app.s.promoters
    await app.event('/start', uid=owner)
    reply = (await app.db.query("SELECT payload FROM outbox WHERE kind IN ('send','ui') ORDER BY id DESC LIMIT 1"))[0]
    assert any(label == 'Добавить лида' and action == 'menu:new'
               for row in json.loads(reply['payload'])['keyboard'] for label, action in row)
    request = await app.intake(uid=owner)
    await app.worker.tick()
    saved = (await app.db.query('SELECT user_id,state,crm_id FROM requests WHERE id=?', (request['id'],)))[0]
    assert saved['user_id'] == owner and saved['state'] == 'delivered' and saved['crm_id']
    await app.event('/start', uid=owner)
    reply = (await app.db.query("SELECT payload FROM outbox WHERE kind IN ('send','ui') ORDER BY id DESC LIMIT 1"))[0]
    assert any(action == 'admin:home' for row in json.loads(reply['payload'])['keyboard'] for _, action in row)
