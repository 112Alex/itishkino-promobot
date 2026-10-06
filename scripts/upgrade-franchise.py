#!/usr/bin/env python3
"""Upgrade this franchise's local runtime files; never touch SQLite or VPN keys."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile

BRANCHES = [('preobrazhenka', 'Преображенка', 4), ('kuzminki', 'Кузьминки', 3),
            ('maryino', 'Марьино', 2), ('zhulebino', 'Жулебино', 1)]


def ids(value):
    result = {int(x.strip()) for x in value.split(',') if x.strip()}
    if any(not 0 < uid <= 2**52 - 1 for uid in result):
        raise ValueError('Некорректный Telegram ID')
    return result


def env_value(text, key):
    found = re.findall(r'^' + re.escape(key) + r'\s*=\s*(.*)$', text, re.M)
    return found[-1].split('#', 1)[0].strip().strip('\"\'') if found else ''


def update_env(text, key, value):
    lines = text.splitlines()
    positions = [i for i, line in enumerate(lines) if re.match(r'^' + re.escape(key) + r'\s*=', line)]
    if positions:
        lines[positions[0]] = key + '=' + value
        for i in reversed(positions[1:]):
            del lines[i]
    else:
        lines.append(key + '=' + value)
    return '\n'.join(lines) + '\n'


def write_private(path, content, owner):
    fd, temporary = tempfile.mkstemp(prefix='.franchise-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, owner.st_uid, owner.st_gid)
        with os.fdopen(fd, 'w') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def upgrade(runtime, extra_admins=(), dry_run=False):
    cfg_path, env_path = runtime / 'config.json', runtime / '.env'
    cfg_text, env_text = cfg_path.read_text(), env_path.read_text()
    cfg, owner_cfg, owner_env = json.loads(cfg_text), cfg_path.stat(), env_path.stat()
    if any(stat.S_IMODE(s.st_mode) & 0o027 for s in (owner_cfg, owner_env)):
        raise ValueError('Настройки должны иметь права 600 или 640; секреты не изменены')
    b = cfg['branch']
    if (b['key'], b['crm_id'], b['pipeline_id'], b['source_id']) != ('preobrazhenka', 4, 1, 6):
        raise ValueError('Скрипт рассчитан на текущую CRM Айтишкино; настройки не изменены')
    configured = {x['key']: x for x in cfg.get('branches', [])}
    if len(configured) != len(cfg.get('branches', [])):
        raise ValueError('Повторные ключи филиалов; настройки не изменены')
    template = {k: v for k, v in b.items() if k != 'acceptance'}
    for key, label, crm_id in BRANCHES:
        if key in configured and configured[key]['crm_id'] != crm_id:
            raise ValueError('CRM ID филиала отличается; настройки не изменены')
        configured.setdefault(key, {**template, 'key': key, 'name': 'Айтишкино — ' + label, 'crm_id': crm_id})
    configured[b['key']] = b
    cfg['branches'] = list(configured.values())
    admins = ids(env_value(env_text, 'ADMIN_TELEGRAM_IDS')) | ids(env_value(env_text, 'ADMIN_TELEGRAM_ID'))
    admins |= {int(uid) for uid in cfg.get('administrators', {})} | {int(uid) for uid in extra_admins}
    ids(','.join(map(str, admins)))
    if not admins:
        raise ValueError('Нужно указать хотя бы одного первоначального администратора')
    # Retain owner first if CSV becomes the only initial-role setting later.
    owner = env_value(env_text, 'ADMIN_TELEGRAM_ID')
    ordered = ([int(owner)] if owner else []) + sorted(admins - ({int(owner)} if owner else set()))
    env_text = update_env(env_text, 'ADMIN_TELEGRAM_IDS', ','.join(map(str, ordered)))
    if dry_run:
        return {'dry_run': True, 'branches': [x[1] for x in BRANCHES], 'initial_admin_count': len(admins)}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    write_private(runtime / f'before-franchise-{stamp}.json', cfg_text, owner_cfg)
    write_private(runtime / f'before-franchise-{stamp}.env', env_path.read_text(), owner_env)
    write_private(cfg_path, json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', owner_cfg)
    write_private(env_path, env_text, owner_env)
    return {'updated': True, 'branches': [x[1] for x in BRANCHES], 'initial_admin_count': len(admins),
            'backup_prefix': f'before-franchise-{stamp}', 'database_changed': False}


def main():
    p = argparse.ArgumentParser(description='Четыре филиала Айтишкино и первоначальные администраторы')
    p.add_argument('--runtime', type=Path, default=Path('runtime'))
    p.add_argument('--admin-id', type=int, action='append', default=[])
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    try:
        print(json.dumps(upgrade(args.runtime, args.admin_id, args.dry_run), ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError):
        p.exit(2, 'Обновление не выполнено. Проверьте закрытые runtime-файлы, ID и конфигурацию текущей CRM. Секреты не выводились.\n')


if __name__ == '__main__':
    main()
