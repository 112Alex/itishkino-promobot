import json
from types import SimpleNamespace
import pytest
from promobot import cli
from promobot.config import ConfigurationError,Settings


async def test_network_check_is_read_only_and_does_not_publish_secrets(app,monkeypatch,capsys):
    app.s.token='42:synthetic'
    methods=[]
    class Bot:
        session=None
        async def get_me(self):methods.append('get_me');return SimpleNamespace(username='synthetic_bot')
        async def get_webhook_info(self):methods.append('get_webhook_info');return SimpleNamespace(url='')
        async def close(self):methods.append('close')
    bot=Bot();bot.session=bot
    monkeypatch.setattr(cli,'make_bot',lambda s:bot)
    await cli.network_check(app.crm,app.s)
    assert methods==['get_me','get_webhook_info','close']
    report=capsys.readouterr().out
    assert '42:synthetic' not in report and 'synthetic_bot' in report
    assert not await app.db.query('SELECT * FROM requests')
    assert not await app.crm.customers()


async def test_webhook_conflict_is_reported_without_deletion(app,monkeypatch):
    app.s.token='42:synthetic'
    class Bot:
        session=None
        async def get_me(self):return SimpleNamespace(username='synthetic_bot')
        async def get_webhook_info(self):return SimpleNamespace(url='https://example.invalid/webhook')
        async def close(self):pass
    bot=Bot();bot.session=bot
    monkeypatch.setattr(cli,'make_bot',lambda s:bot)
    with pytest.raises(ConfigurationError):await cli.network_check(app.crm,app.s)


def test_admin_ids_environment_and_no_default_promoters(tmp_path,monkeypatch):
    cfg=json.loads(open('config/preobrazhenka.example.json').read())
    p=tmp_path/'config.json';p.write_text(json.dumps(cfg))
    monkeypatch.setenv('CONFIG_PATH',str(p));monkeypatch.setenv('ADMIN_TELEGRAM_ID','10001')
    monkeypatch.setenv('ADMIN_TELEGRAM_IDS','10005,10006,10001')
    monkeypatch.setenv('ENVIRONMENT','test');monkeypatch.setenv('CRM_MODE','mock')
    s=Settings.load()
    assert s.admin_ids==(10001,10005,10006) and not s.promoters
    monkeypatch.setenv('ADMIN_TELEGRAM_IDS','nope')
    with pytest.raises(ConfigurationError):Settings.load()
