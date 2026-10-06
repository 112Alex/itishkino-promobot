import json
import sqlite3
from aiohttp import web
import httpx
import pytest
from promobot.config import ConfigurationError
from promobot.crm import AlfaCRM
from promobot.demo import acceptance
from promobot.storage import Store
from promobot.worker import Worker


async def test_acceptance_through_real_local_http_server(app, capsys):
    runner = web.AppRunner(app.mock.application(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = next(iter(runner.addresses))[1]
    crm = AlfaCRM(app.s, httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False))
    crm.limiter.interval = 0
    app.s.token = "42:synthetic-format-only"
    try:
        await acceptance(app.db, app.s, crm)
        assert (await app.db.query("SELECT state FROM requests"))[0]["state"] == "delivered"
        assert len(await crm.customers()) == 1
        assert '"mode":"mock"' in capsys.readouterr().out
    finally:
        await crm.close()
        await runner.cleanup()


async def test_real_acceptance_needs_explicit_flag_and_test_environment(app):
    app.s.mode = "real"
    with pytest.raises(ConfigurationError):
        await acceptance(app.db, app.s, app.crm)
    app.s.environment = "prod"
    with pytest.raises(ConfigurationError):
        await acceptance(app.db, app.s, app.crm, True)
    assert not await app.db.query("SELECT * FROM requests")


async def test_stale_resume_no_silent_overwrite(app):
    await app.event(action="menu:new")
    await app.event("Анна")
    await app.db.execute("UPDATE drafts SET updated_at='2020-01-01T00:00:00+00:00'")
    await app.event("ignored input")
    d = await app.draft()
    assert d["step"] == "replace" and d["data"]["parent"] == "Анна"
    await app.event(action="resume")
    assert (await app.draft())["step"] == "contact_type"
    assert "_stale" not in (await app.draft())["data"]


async def test_stale_after_start_and_continue(app):
    await app.event(action="menu:new")
    await app.event("Анна")
    await app.db.execute("UPDATE drafts SET updated_at='2020-01-01T00:00:00+00:00'")
    await app.event("/start")
    await app.event(action="menu:continue")
    assert (await app.draft())["step"] == "replace"
    await app.event(action="resume")
    assert (await app.draft())["step"] == "contact_type"


async def test_confirmed_settings_change_blocks_delivery(app):
    await app.intake()
    app.s.branch["source_id"] = 999
    await app.worker.tick()
    assert (await app.db.query("SELECT error FROM requests"))[0]["error"] == "configuration_changed"
    assert not await app.crm.customers()


async def test_delivery_attempt_history_and_timezone(app):
    await app.intake()
    await app.worker.tick()
    r = (await app.db.query("SELECT * FROM requests"))[0]
    attempts = await app.db.query("SELECT * FROM delivery_attempts")
    assert len(attempts) == 1 and attempts[0]["outcome"] == "completed"
    assert attempts[0]["started_at"].endswith("+00:00")
    assert r["confirmed_at"] <= r["saved_at"] <= r["verified_at"]
    assert "crm_created_at" not in r


async def test_migration_pre_backup_keeps_existing_draft(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as c:
        c.executescript("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);" +
                       open("src/promobot/migrations/001_initial.sql").read() +
                       "INSERT INTO schema_migrations VALUES(1);INSERT INTO metadata VALUES('kept','value');")
    db = await Store(path).open()
    assert await db.query("SELECT value FROM metadata WHERE key='kept'") == [{"value": "value"}]
    assert len(await db.query("SELECT * FROM schema_migrations")) == 5
    assert (tmp_path / "before-migration-2.sqlite3").exists()
    with sqlite3.connect(tmp_path / "before-migration-2.sqlite3") as c:
        assert c.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    await db.close()


async def test_inbox_keeps_only_needed_fields(app):
    u = {"update_id": 99, "message": {"message_id": 9, "date": 1790992800,
         "from": {"id": 10002, "first_name": "private name", "username": "private_username"},
         "chat": {"id": 10002, "type": "private", "first_name": "private"},
         "photo": [{"file_id": "private-media-id"}], "forward_origin": {"private": True}}}
    await app.db.ingest("42", [u])
    payload = (await app.db.query("SELECT payload FROM inbox"))[0]["payload"]
    assert "file_id" not in payload and "forward_origin" not in payload
    assert json.loads(payload)["message"]["from"]["first_name"] == "private name"
    assert "first_name" not in json.loads(payload)["message"]["chat"]
    assert json.loads(payload)["message"]["from"]["username"] == "private_username"
    await app.dialog.process(99)
    assert (await app.db.query("SELECT state FROM inbox"))[0]["state"] == "done"


async def test_actual_restore_api_readonly_source(app, tmp_path):
    await app.intake(confirm=False)
    source = tmp_path / "copy.sqlite3"
    await app.db.backup(source)
    before = source.read_bytes()
    restored_path = tmp_path / "new.sqlite3"
    await Store.restore(source, restored_path)
    assert source.read_bytes() == before
    restored = await Store(restored_path).open()
    assert await restored.query("SELECT * FROM drafts") == await app.db.query("SELECT * FROM drafts")
    await restored.close()
    with pytest.raises(FileExistsError):
        await Store.restore(source, restored_path)


def test_real_credentials_reject_insecure_files(tmp_path):
    # Synchronous tests build their own settings; no credentials are real.
    from promobot.config import Settings, guard_permissions
    config = json.loads(open("config/mock.json").read())
    s = Settings("test", "mock", tmp_path / "state" / "bot.sqlite3", config["branch"], {}, 1,
                 token="42:fake-format-only")
    env = tmp_path / "settings.env"
    cfg = tmp_path / "settings.json"
    env.write_text("no secret")
    cfg.write_text("{}")
    env.chmod(0o644)
    cfg.chmod(0o600)
    with pytest.raises(ConfigurationError):
        guard_permissions(s, env, cfg)
    env.chmod(0o600)
    guard_permissions(s, env, cfg)
