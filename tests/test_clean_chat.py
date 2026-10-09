import json
import sqlite3
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import DeleteMessage
from promobot.telegram import Outbox
from test_multibranch import UIBot, drain, franchise, latest


async def test_user_messages_are_deleted_only_after_committed_processing(app):
    franchise(app)
    await app.event(callback='branch:select:kuzminki')
    await app.event('/new')
    await app.event('Анна Алиса 9 +79991234567')
    class Bot(UIBot):
        async def delete_message(self, chat_id, message_id):
            event = (await app.db.query('SELECT state FROM inbox WHERE update_id=?', (message_id,)))[0]
            assert event['state'] == 'done'
            assert json.loads((await app.db.query('SELECT data FROM requests'))[0]['data'])['parent'] == 'Анна'
            await super().delete_message(chat_id, message_id)
    bot = Bot()
    await drain(Outbox(app.db, bot, app.s))
    assert bot.deleted == [(10002, 2), (10002, 3)]
    assert len(bot.sent) == 1
    assert (await app.db.query('SELECT branch_key FROM requests'))[0]['branch_key'] == 'kuzminki'


async def test_failed_transaction_never_enqueues_deletion_then_replay_dedupes(app, monkeypatch):
    import promobot.dialog as module
    await app.event('/new')
    original = module.enqueue
    async def fail(*args, **kw):
        if kw.get('kind') == 'delete':
            raise sqlite3.OperationalError('synthetic disk failure')
        return await original(*args, **kw)
    monkeypatch.setattr(module, 'enqueue', fail)
    with pytest.raises(sqlite3.OperationalError):
        await app.event('Анна Алиса 9 +79991234567')
    assert (await app.draft())['step'] == 'message'
    assert 'parent' not in (await app.draft())['data']
    assert len(await app.db.query("SELECT * FROM outbox WHERE kind='delete'")) == 1
    pending = (await app.db.query("SELECT update_id FROM inbox WHERE state='pending'"))[0]['update_id']
    monkeypatch.setattr(module, 'enqueue', original)
    await app.dialog.process(pending)
    await app.dialog.process(pending)
    assert len(await app.db.query("SELECT * FROM outbox WHERE kind='delete'")) == 2
    assert json.loads((await app.db.query('SELECT data FROM requests'))[0]['data'])['parent'] == 'Анна'


async def test_delete_retry_does_not_block_menu_and_survives_new_outbox(app):
    class Bot(UIBot):
        fail = True
        async def delete_message(self, chat_id, message_id):
            if self.fail:
                self.fail = False
                raise TelegramRetryAfter(method=DeleteMessage(chat_id=chat_id, message_id=message_id), message='synthetic', retry_after=60)
            await super().delete_message(chat_id, message_id)
    bot = Bot()
    out = Outbox(app.db, bot, app.s)
    await app.event('/start')
    assert await out.tick()  # Send persistent menu.
    assert await out.tick()  # Delete waits to retry.
    await app.event(action='menu:new')
    await drain(out)
    assert len(bot.edited) == 1 and not bot.deleted
    await app.db.execute("UPDATE outbox SET next_at=0 WHERE kind='delete'")
    await drain(Outbox(app.db, bot, app.s))
    assert bot.deleted == [(10002, 1)]
    assert not await app.db.query("SELECT * FROM outbox WHERE state IN ('pending','held')")


async def test_missing_user_message_is_finished_without_alert_spam(app):
    class Bot(UIBot):
        async def delete_message(self, chat_id, message_id):
            raise TelegramBadRequest(method=DeleteMessage(chat_id=chat_id, message_id=message_id), message='message to delete not found')
    await app.event('/start')
    await drain(Outbox(app.db, Bot(), app.s))
    assert not await app.db.query("SELECT * FROM outbox WHERE dedupe LIKE 'alert:%'")
    assert (await app.db.query("SELECT state FROM outbox WHERE kind='delete'")) == [{'state': 'done'}]


async def test_only_incoming_private_messages_are_deleted_and_admin_inputs_are_included(app):
    await app.event('/new', chat_type='group')
    await app.event('/start')
    await app.event(action='menu:new')
    await app.event('/add_promoter 30001', uid=app.s.admin)
    await app.event('Максим', uid=app.s.admin)
    deletes = await app.db.query("SELECT chat_id,payload FROM outbox WHERE kind='delete'")
    assert [(r['chat_id'], json.loads(r['payload'])['text']) for r in deletes] == [(10002, '2'), (10001, '4'), (10001, '5')]


async def test_old_branch_step_is_skipped_and_edit_menu_has_no_branch_button(app):
    franchise(app)
    await app.event('/new')
    await app.db.execute("UPDATE drafts SET step='branch'")
    await app.event('Анна')
    assert (await app.draft())['step'] == 'contact_type'
    await app.event(action='back')
    assert (await app.draft())['step'] == 'parent'
    await app.event(action='cancel')
    await app.intake(confirm=False)
    await app.event(action='edit')
    assert not any(action.endswith(':ebranch') for row in (await latest(app))['keyboard'] for _, action in row)


async def test_unsupported_attachment_is_not_deleted_without_saved_content(app):
    await app.event(None)
    assert not await app.db.query("SELECT * FROM outbox WHERE kind='delete'")
