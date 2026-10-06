"""Current DB roles and per-admin branch subscriptions, read in the caller's transaction."""
import json
from .storage import one, rows


async def administrators(c, settings, branch_key=None):
    identity = await one(c, "SELECT value FROM metadata WHERE key='identity'")
    bot_id = str(json.loads(identity['value'])[1]) if identity else None
    found = await rows(c, 'SELECT user_id FROM administrators WHERE active=1' + (' AND bot_id=?' if bot_id else ''), (bot_id,) if bot_id else ())
    result = set(settings.admin_ids) | {r['user_id'] for r in found}
    if branch_key is None:
        return sorted(result)
    watches = await rows(c, 'SELECT user_id,branches FROM admin_watches' + (' WHERE bot_id=?' if bot_id else ''), (bot_id,) if bot_id else ())
    selected = {r['user_id']: json.loads(r['branches']) for r in watches}
    return sorted(uid for uid in result if branch_key in selected.get(uid, list(settings.all_branches)))
