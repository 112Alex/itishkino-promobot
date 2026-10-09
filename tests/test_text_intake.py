import asyncio
import json
import pytest
from promobot.domain import InputError
from promobot.intake import parse_message
from promobot.telegram import Outbox
from test_multibranch import UIBot, drain, franchise, latest


@pytest.mark.parametrize('header,expected', [
    ('Дарья Максим 8 Оля 7 +79991234567', [('Максим', 8), ('Оля', 7)]),
    ('Иван Софья 10 @my_parent', [('Софья', 10)]),
    ('Мария Игорь 6 Василий 8 8 (999) 123-45-67', [('Игорь', 6), ('Василий', 8)]),
    ('"Анна Мария" "Иван Петров" 9 +44 20 7946 0958', [('Иван Петров', 9)]),
    ('Мария Игорь ? +79991234567', [('Игорь', None)]),
])
def test_parse_families_and_formatted_contacts(app, header, expected):
    d = parse_message(header + '\nПервая строка комментария\nВторая: +79990000000 звонить', app.s)
    assert [(c['name'], c['age']) for c in d['children']] == expected
    assert d['comment'] == 'Первая строка комментария\nВторая: +79990000000 звонить'
    assert '+79990000000' not in d['phones']


@pytest.mark.parametrize('tail,phones,users,pref', [
    ('+79991234567', ['+79991234567'], [], 'Можно оба способа'),
    ('8 (999) 123-45-67 писать MAX Telegram', ['+79991234567'], [], 'Только писать'),
    ('https://t.me/My_Parent', [], ['@my_parent'], 'Только писать'),
    ('+79991234567 @My_Parent звонить', ['+79991234567'], ['@my_parent'], 'Только звонить'),
    ('+79991234567 +79991234568 @my_parent @other_parent WhatsApp писать/звонить',
        ['+79991234567', '+79991234568'], ['@my_parent', '@other_parent'], 'Можно оба способа'),
])
def test_parse_contacts_and_explicit_preferences(app, tail, phones, users, pref):
    d = parse_message('Мария Игорь 6 ' + tail, app.s)
    assert d['phones'] == phones and d['usernames'] == users and d['preference'] == pref


@pytest.mark.parametrize('text', [
    'Дарья Максим 8 Оля 7', 'Иван Софья 10', 'Мария Игорь 6\n+79991234567',
    'Мария Игорь +79991234567', 'Мария Игорь 90 +79991234567',
    'Мария Игорь 1.5 +79991234567', '+79991234567 Игорь 6',
    'Мария +79991234567', 'Мария Игорь 6 +12345',
    'Мария Игорь 6 @bad', 'Мария Игорь 6 @my_parent звонить',
    'Мария Игорь 6 @my_parent MAX', 'Мария Игорь 6 +79991234567 писать звонить',
    'Мария Игорь 6 +79991234567 какой-то текст', '"Мария Игорь 6 +79991234567',
    'Мария Игорь 6 +79991234567 +79991234568 +79991234569',
])
def test_invalid_message_is_rejected(app, text):
    with pytest.raises(InputError):
        parse_message(text, app.s)


async def test_submit_without_start_or_buttons_and_delete_after_durable_queue(app):
    u = await app.event('Мария Игорь 6 Василий 8 +79991234567 @my_parent\nНе звонить утром')
    r = (await app.db.query('SELECT * FROM requests'))[0]
    d = json.loads(r['data'])
    assert d['parent'] == 'Мария' and len(d['children']) == 2 and d['comment'] == 'Не звонить утром'
    assert r['state'] == 'queued' and not await app.draft()
    assert len(await app.db.query('SELECT * FROM jobs')) == 1
    assert not (await latest(app))['keyboard']
    await app.db.ingest('42', [u])
    await app.dialog.process(u['update_id'])
    assert len(await app.db.query('SELECT * FROM requests')) == 1
    bot = UIBot()
    await drain(Outbox(app.db, bot, app.s))
    assert bot.deleted == [(10002, u['message']['message_id'])]
    await app.worker.tick()
    r = (await app.db.query('SELECT * FROM requests'))[0]
    assert r['state'] == 'delivered'
    model = (await app.crm.customers())[0]
    assert model['name'] == 'Мария Игорь 6 Василий 8' and model['note'].endswith('писать/звонить')
    assert 'Не звонить утром' in (await app.crm.comments(model['id']))[0]['comment']


async def test_branch_preferences_are_independent_for_simultaneous_promoters(app):
    franchise(app)
    await app.event(callback='branch:select:kuzminki', uid=10003)
    await asyncio.gather(app.event('Анна Алиса 9 +79991234567', uid=10002),
                         app.event('Ирина Оля 7 +79991234567', uid=10003))
    await asyncio.gather(app.worker.tick(), app.worker.tick())
    found = await app.db.query('SELECT user_id,branch_key,state FROM requests ORDER BY user_id')
    assert found == [{'user_id': 10002, 'branch_key': 'preobrazhenka', 'state': 'delivered'},
                     {'user_id': 10003, 'branch_key': 'kuzminki', 'state': 'delivered'}]
    assert len(await app.crm.customers()) == len(await app.crm.for_branch('kuzminki').customers()) == 1


async def test_invalid_message_does_not_send_or_overwrite_existing_draft(app):
    await app.intake(confirm=False)
    before = (await app.draft())['data']
    await app.event('Мария Игорь 6 +12345')
    assert not await app.db.query('SELECT * FROM requests')
    assert (await app.draft())['data'] == before
    assert 'Неверный формат телефона' in (await latest(app))['text']
    await app.event('Дарья Максим 8 Оля 7')
    assert 'Добавьте телефон' in (await latest(app))['text']
    assert (await app.draft())['data'] == before
    await app.event('Мария Игорь 6 +79991234567')
    assert len(await app.db.query('SELECT * FROM requests')) == 1 and not await app.draft()


async def test_all_admins_can_submit_using_same_format_and_groups_cannot(app):
    app.s.administrators = {10005: 'preobrazhenka'}
    await app.event('Анна Алиса 9 +79991234567', uid=10005)
    assert (await app.db.query('SELECT user_id FROM requests')) == [{'user_id': 10005}]
    await app.event('Ирина Оля 7 +79991234568', uid=app.s.admin, chat_type='group')
    await app.event('Ирина Оля 7 +79991234568', uid=99999)
    assert len(await app.db.query('SELECT * FROM requests')) == 1


async def test_large_title_is_shortened_only_in_heading_and_comment_is_not_interpreted(app):
    d = parse_message('"Анна Мария" "Очень длинное имя ребёнка с несколькими словами" 9 +79991234567\nИгорь умный парень, но у него аутизм\nЗаголовок: не менять\nписать', app.s)
    assert len(d['title']) <= app.s.name_limit and d['title'].endswith('…')
    await app.event('"Анна Мария" "Очень длинное имя ребёнка с несколькими словами" 9 +79991234567\nИгорь умный парень, но у него аутизм\nЗаголовок: не менять\nписать')
    await app.worker.tick()
    m = (await app.crm.customers())[0]
    comment = (await app.crm.comments(m['id']))[0]['comment']
    assert 'Очень длинное имя ребёнка с несколькими словами' in comment
    assert 'Заголовок: не менять\nписать' in comment and m['note'].endswith('писать/звонить')


async def test_start_exposes_only_branch_button_and_admin_command_instructions(app):
    franchise(app)
    await app.event('/start', uid=app.s.admin)
    p = await latest(app)
    assert '/admin' in p['text']
    assert [a for row in p['keyboard'] for _, a in row] == ['menu:branches']
    await app.event('/admin', uid=app.s.admin)
    assert not (await latest(app))['keyboard']
    assert '/add_promoter' in (await latest(app))['text']
