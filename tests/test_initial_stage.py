import json
import pytest
from promobot.crm import CRMError
from promobot.domain import crm_payload


async def test_initial_stage_payload_and_readback(app, monkeypatch):
    request = await app.intake()
    request["data"] = json.loads(request["data"])
    app.s.branch.update(initial_unassigned=True, status_id=None, pipeline_id=1)
    payload = crm_payload(request, app.s)
    assert 'lead_status_ids' not in payload
    assert 'lead_status_id' not in payload
    assert payload['pipeline_id'] == 1
    record = dict(payload, id=77, lead_status_id=None)
    async def index(*args, **kwargs):
        return [record]
    monkeypatch.setattr(app.crm, 'index', index)
    assert (await app.crm.verify(77, payload))['id'] == 77
    record['lead_status_id'] = 2
    with pytest.raises(CRMError, match='verify_mismatch'):
        await app.crm.verify(77, payload)
    del record['lead_status_id']
    with pytest.raises(CRMError, match='verify_mismatch'):
        await app.crm.verify(77, payload)


async def test_null_stage_does_not_bypass_unverified_contract(app):
    app.s.branch.update(initial_unassigned=True, status_id=None, verified_contract=False)
    app.s.crm_email = 'test@example.invalid'
    app.s.crm_key = 'synthetic'
    from promobot.config import ConfigurationError
    with pytest.raises(ConfigurationError):
        app.s.require_real_contract()


async def test_initial_unassigned_mock_delivery(app):
    app.s.branch.update(initial_unassigned=True, status_id=None, pipeline_id=1)
    request = await app.intake()
    await app.worker.tick()
    saved = (await app.db.query('SELECT state,crm_id FROM requests WHERE id=?', (request['id'],)))[0]
    assert saved['state'] == 'delivered'
    cards = await app.mock.store.query("SELECT body FROM mock_models WHERE kind='customer'")
    assert len(cards) == 1
    assert json.loads(cards[0]['body'])['lead_status_id'] is None
