import asyncio
import json
import logging
import time
import httpx
import pytest
from promobot.crm import AlfaCRM, CRMError, Limiter
from promobot.domain import communication, crm_payload
from promobot.storage import dumps
from promobot.worker import Worker


async def state(app):
    return (await app.db.query("SELECT state FROM requests ORDER BY saved_at LIMIT 1"))[0]["state"]


@pytest.mark.parametrize("kind", ["lead", "client", "archive", "username", "second_phone"])
async def test_old_contacts_never_modified(app, kind):
    r = await app.intake(telegram="@my_parent")
    payload = {"name": "Old", "branch_ids": [901], "phone": ["+79991234567"], "web": [], "is_study": 0, "removed": 0}
    if kind == "client":
        payload["is_study"] = 1
    if kind == "archive":
        payload["removed"] = 1
    if kind == "username":
        payload["phone"], payload["web"] = [], ["https://t.me/MY_PARENT"]
    if kind == "second_phone":
        payload["phone"] = ["+79991234568", "+79991234567"]
    model = await app.crm.create(payload)
    await app.worker.tick()
    assert await state(app) == "duplicate_review"
    assert await app.crm.customers() == [model]
    assert await app.crm.comments(model["id"]) == []
    await app.event("/retry " + r["id"], uid=app.s.admin)
    assert (await app.db.query("SELECT state FROM jobs"))[0]["state"] == "done"


async def test_same_contact_two_promoters(app):
    await app.intake()
    await app.intake(uid=10003)
    await asyncio.gather(app.worker.tick(), app.worker.tick())
    assert len(await app.crm.customers()) == 1
    states = {r["state"] for r in await app.db.query("SELECT state FROM requests")}
    assert states == {"delivered", "duplicate_review"}


async def test_create_lost_response_reconciles_without_second_create(app):
    r = await app.intake()
    original = app.crm.create
    calls = 0
    async def lost(payload):
        nonlocal calls
        calls += 1
        await original(payload)
        raise CRMError("network", True, True)
    app.crm.create = lost
    await app.worker.tick()
    assert await state(app) == "retry_wait"
    assert (await app.db.query("SELECT phase FROM jobs"))[0]["phase"] == "reconcile_create"
    await app.db.execute("UPDATE jobs SET next_at=0")
    await app.worker.tick()
    assert await state(app) == "delivered"
    assert calls == 1 and len(await app.crm.customers()) == 1


async def test_uncertain_empty_search_goes_manual_never_blind_create(app):
    await app.intake()
    calls = 0
    async def uncertain(payload):
        nonlocal calls
        calls += 1
        raise CRMError("network", True, True)
    app.crm.create = uncertain
    for _ in range(app.s.max_attempts):
        await app.db.execute("UPDATE jobs SET next_at=0")
        await app.worker.tick()
    assert calls == 1 and await state(app) == "manual_review"
    r = (await app.db.query("SELECT id FROM requests"))[0]
    await app.event("/retry " + r["id"], uid=app.s.admin)
    await app.worker.tick()
    assert calls == 1
    notices = await app.db.query("SELECT * FROM outbox WHERE dedupe LIKE '%retry_wait:admin'")
    assert len(notices) == 1


async def test_incomplete_search_prevents_create(app):
    await app.intake()
    original = app.mock.handle
    async def broken(req):
        if req.url.path.endswith("customer/index"):
            return httpx.Response(200, json={"page": 0, "total": 3, "count": 0, "items": []})
        return await original(req)
    app.crm.client = httpx.AsyncClient(transport=httpx.MockTransport(broken), base_url="https://mock.invalid")
    await app.worker.tick()
    assert await state(app) == "retry_wait"
    assert not await app.mock.store.query("SELECT * FROM mock_models WHERE kind='customer'")
    assert (await app.db.query("SELECT phase FROM jobs"))[0]["phase"] == "check"


async def test_partial_comment_retries_only_comment(app):
    await app.intake()
    original = app.crm.add_comment
    n = 0
    async def fail_once(*args):
        nonlocal n
        n += 1
        if n == 1:
            raise CRMError("http_429", True)
        return await original(*args)
    app.crm.add_comment = fail_once
    await app.worker.tick()
    assert len(await app.crm.customers()) == 1
    await app.db.execute("UPDATE jobs SET next_at=0")
    await app.worker.tick()
    assert await state(app) == "delivered"
    assert len(await app.crm.comments(1)) == 1


async def test_comment_lost_response_no_second_comment(app):
    await app.intake()
    original = app.crm.add_comment
    calls = 0
    async def lost(*args):
        nonlocal calls
        calls += 1
        await original(*args)
        raise CRMError("network", True, True)
    app.crm.add_comment = lost
    await app.worker.tick()
    await app.db.execute("UPDATE jobs SET next_at=0")
    await app.worker.tick()
    assert calls == 1 and await state(app) == "delivered"
    assert len(await app.crm.comments(1)) == 1


async def test_uncertain_comment_stays_uncertain_across_retries(app):
    await app.intake()
    calls = 0
    async def lost(*args):
        nonlocal calls
        calls += 1
        raise CRMError("network", True, True)
    app.crm.add_comment = lost
    for _ in range(app.s.max_attempts):
        await app.db.execute("UPDATE jobs SET next_at=0")
        await app.worker.tick()
    assert calls == 1 and await state(app) == "manual_review"
    assert (await app.db.query("SELECT phase FROM jobs"))[0]["phase"] == "reconcile_comment"


async def test_restart_processing_recovers_and_verifies(app):
    r = await app.intake()
    r["data"] = json.loads(r["data"])
    model = await app.crm.create(crm_payload(r, app.s))
    await app.db.execute("UPDATE jobs SET state='processing',phase='reconcile_create'")
    worker = Worker(app.db, app.s, app.crm)
    await worker.recover()
    await worker.tick()
    assert await state(app) == "delivered" and len(await app.crm.customers()) == 1


async def test_restart_before_create_delivers_saved_request(app):
    await app.intake()
    worker = Worker(app.db, app.s, app.crm)
    await worker.recover()
    await worker.tick()
    assert await state(app) == "delivered"


async def test_unresolved_contact_blocks_later_request(app):
    await app.intake()
    await app.intake(uid=10003)
    async def lost(payload):
        raise CRMError("network", True, True)
    app.crm.create = lost
    await app.worker.tick()
    await app.db.execute("UPDATE jobs SET next_at=9999999999 WHERE phase='reconcile_create'")
    await app.worker.tick()
    rows = await app.db.query("SELECT state,error FROM requests ORDER BY saved_at")
    assert rows[1] == {"state": "manual_review", "error": "contact_operation_unresolved"}


@pytest.mark.parametrize("status,body,code,retry,ambiguous", [
    (200, {"success": False, "errors": {"name": ["bad"]}}, "crm_validation", False, False),
    (200, {"success": True, "model": {}}, "write_result_missing", True, True),
    (401, {}, "http_401", False, False), (403, {}, "http_403", False, False),
    (429, {}, "http_429", True, False), (503, {}, "http_5xx", True, True),
    (400, {}, "http_4xx", False, False),
])
async def test_http_error_classification(app, status, body, code, retry, ambiguous):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)), base_url="https://mock.invalid")
    crm = AlfaCRM(app.s, client)
    crm.limiter.interval = 0
    with pytest.raises(CRMError) as error:
        await crm.raw("/write", {}, write=True)
    assert (error.value.code, error.value.retryable, error.value.ambiguous) == (code, retry, ambiguous)
    await crm.close()


async def test_timeout_and_bad_json(app):
    async def timeout(req):
        raise httpx.ReadTimeout("TOKEN CONTACT SHOULD NEVER BE LOGGED")
    client = httpx.AsyncClient(transport=httpx.MockTransport(timeout), base_url="https://mock.invalid")
    crm = AlfaCRM(app.s, client)
    with pytest.raises(CRMError) as err:
        await crm.raw("/write", {}, True)
    assert str(err.value) == "network" and err.value.ambiguous
    await crm.close()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="garbage")), base_url="https://mock.invalid")
    crm = AlfaCRM(app.s, client)
    with pytest.raises(CRMError) as err:
        await crm.raw("/write", {}, True)
    assert err.value.ambiguous
    await crm.close()


async def test_auth_cache_refresh_and_403_limit(app):
    calls = []
    reject = 401
    def handler(req):
        nonlocal reject
        calls.append(req.url.path)
        if req.url.path.endswith("login"):
            return httpx.Response(200, json={"token": "T"})
        if reject:
            current, reject = reject, 0
            return httpx.Response(current)
        return httpx.Response(200, json={"items": [], "page": 0, "count": 0, "total": 0})
    crm = AlfaCRM(app.s, httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mock.invalid"))
    crm.limiter.interval = 0
    await crm.call("/read", {})
    await crm.call("/read", {})
    assert calls.count("/v2api/auth/login") == 2
    reject = 403
    with pytest.raises(CRMError):
        await crm.call("/read", {})
    assert calls.count("/v2api/auth/login") == 2
    await crm.close()


async def test_limiter_uniform_no_burst():
    limiter = Limiter(.02)
    stamps = []
    async def request():
        await limiter.wait()
        stamps.append(time.monotonic())
    await asyncio.gather(*(request() for _ in range(4)))
    assert all(b-a >= .018 for a, b in zip(stamps, stamps[1:]))


async def test_crm_automation_conflict_no_false_delivery(app):
    await app.intake()
    original = app.crm.create
    async def assign(payload):
        return await original({**payload, "assigned_id": 123})
    app.crm.create = assign
    await app.worker.tick()
    assert await state(app) == "manual_review"
    assert await app.crm.comments(1) == []


async def test_log_redaction(app, caplog):
    await app.intake(comment="Нельзя сладкое secret-health")
    async def timeout(payload):
        raise CRMError("network", True, True)
    app.crm.create = timeout
    with caplog.at_level(logging.INFO, logger="promobot"):
        await app.worker.tick()
    assert "network" in caplog.text
    for forbidden in ("secret-health", "Нельзя сладкое", "79991234567", "Алиса", "api_key"):
        assert forbidden not in caplog.text


@pytest.mark.parametrize("case", ["total_changes", "duplicate_id", "wrong_branch", "bad_count"])
async def test_untrustworthy_pagination_blocks_create(app, case):
    await app.intake()
    def handler(req):
        if req.url.path.endswith("login"):
            return httpx.Response(200, json={"token": "mock-only"})
        page = json.loads(req.content).get("page", 0)
        item = {"id": 1 if case == "duplicate_id" else page+1, "branch_ids": [999 if case == "wrong_branch" else 901], "phone": [], "web": []}
        total = 1 if case == "wrong_branch" else 2 + (page if case == "total_changes" else 0)
        return httpx.Response(200, json={"total": total, "count": 3 if case == "bad_count" else 1, "page": page, "items": [item]})
    crm = AlfaCRM(app.s, httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mock.invalid"))
    crm.limiter.interval = 0
    worker = Worker(app.db, app.s, crm)
    await worker.tick()
    assert await state(app) in {"manual_review", "retry_wait"}
    assert not await app.mock.store.query("SELECT * FROM mock_models")
    await crm.close()
