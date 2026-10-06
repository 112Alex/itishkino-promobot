import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest
from promobot.cli import branch_check
from promobot.config import ConfigurationError
from promobot.storage import Store

spec = importlib.util.spec_from_file_location('franchise_upgrade', 'scripts/upgrade-franchise.py')
upgrade_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade_module)


def files(tmp_path):
    cfg = json.loads(Path('config/preobrazhenka.example.json').read_text())
    cfg['promoters'] = {'20001': 'preobrazhenka'}
    cfg['administrators'] = {'10003': 'preobrazhenka'}
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(cfg))
    env = tmp_path / '.env'
    env.write_text('TELEGRAM_BOT_TOKEN=fake-secret\nCRM_API_KEY=fake-key\nADMIN_TELEGRAM_ID=10001\nADMIN_TELEGRAM_IDS=10002\nTELEGRAM_PROXY=socks5://vpn:1080\n')
    env.chmod(0o600)
    config.chmod(0o600)
    return env, config


def test_upgrade_preserves_credentials_roles_metadata_and_is_repeatable(tmp_path):
    env, config = files(tmp_path)
    original = config.read_text()
    report = upgrade_module.upgrade(tmp_path, [10004])
    assert report['initial_admin_count'] == 4 and report['database_changed'] is False
    cfg = json.loads(config.read_text())
    assert [(b['key'], b['crm_id']) for b in cfg['branches']] == [('preobrazhenka', 4), ('kuzminki', 3), ('maryino', 2), ('zhulebino', 1)]
    assert cfg['promoters'] == {'20001': 'preobrazhenka'}
    assert 'fake-secret' in env.read_text() and 'fake-key' in env.read_text() and 'socks5://vpn:1080' in env.read_text()
    assert 'ADMIN_TELEGRAM_IDS=10001,10002,10003,10004' in env.read_text()
    assert (tmp_path / (report['backup_prefix'] + '.json')).read_text() == original
    assert env.stat().st_mode & 0o777 == 0o600
    result = config.read_text(), env.read_text()
    upgrade_module.upgrade(tmp_path, [10004])
    assert result == (config.read_text(), env.read_text())


def test_upgrade_dry_run_and_wrong_branch_do_not_mutate(tmp_path):
    env, config = files(tmp_path)
    before = env.read_bytes(), config.read_bytes()
    upgrade_module.upgrade(tmp_path, [10004], dry_run=True)
    assert before == (env.read_bytes(), config.read_bytes())
    cfg = json.loads(config.read_text())
    cfg['branches'] = [{**cfg['branch'], 'key': 'maryino', 'crm_id': 99}]
    config.write_text(json.dumps(cfg))
    before = env.read_bytes(), config.read_bytes()
    with pytest.raises(ValueError):
        upgrade_module.upgrade(tmp_path, [10004])
    assert before == (env.read_bytes(), config.read_bytes())
    assert not list(tmp_path.glob('before-franchise-*'))


async def test_read_only_branch_check_uses_every_branch_without_contact_output(app, capsys):
    from test_multibranch import franchise
    franchise(app)
    await app.crm.create({'name': 'Private family', 'phone': ['+79991234567'], 'branch_ids': [901], app.s.branch['request_field']: 'opaque'})
    await branch_check(app.crm, app.s)
    text = capsys.readouterr().out
    report = json.loads(text)
    assert report['read_only'] and len(report['branches']) == 4
    assert '+79991234567' not in text and 'Private family' not in text
    assert len(await app.crm.customers()) == 1


async def test_migration_from_v4_keeps_legacy_roles_and_identity(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)')
        for migration in sorted(Path('src/promobot/migrations').glob('*.sql'))[:4]:
            c.executescript(migration.read_text())
            c.execute('INSERT INTO schema_migrations VALUES(?)', (int(migration.name[:3]),))
        c.execute("INSERT INTO promoters VALUES('42','preobrazhenka',20001,1,3,10001,'2026-10-05T00:00:00+00:00')")
        c.execute("INSERT INTO metadata VALUES('identity',?)", (json.dumps(['test', '42', 'preobrazhenka', 901, 'mock', 'https://itishkino.s20.online']),))
    db = await Store(path).open()
    assert (await db.query('SELECT active,revision,display_name FROM promoters')) == [{'active': 1, 'revision': 3, 'display_name': None}]
    assert (tmp_path / 'before-migration-5.sqlite3').exists()
    with sqlite3.connect(tmp_path / 'before-migration-5.sqlite3') as c:
        assert c.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 4
        assert c.execute('SELECT revision FROM promoters').fetchone()[0] == 3
    await db.close()
