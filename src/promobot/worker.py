import asyncio
import json
import logging
import random
import time
from .crm import CRMError
from .domain import InputError, communication, contact_keys, crm_payload, moscow, now
from .storage import dumps, enqueue, one
from .access import administrators

log = logging.getLogger("promobot")


class Worker:
    def __init__(self, store, settings, crm):
        self.db, self.s, self.crm = store, settings, crm
        self.lock = asyncio.Lock()  # single serialized CRM worker, including check/create

    async def recover(self):
        # Phase was written BEFORE the external operation. Never infer it from a timeout.
        await self.db.execute("UPDATE jobs SET state='pending',next_at=0 WHERE state='processing'")
        await self.db.execute("UPDATE outbox SET state='pending',next_at=0 WHERE state='processing'")

    async def change(self, ident, state, phase=None, **fields):
        async with self.db.tx() as c:
            columns = {"state": state, **fields}
            await c.execute("UPDATE requests SET " + ",".join(k + "=?" for k in columns) + " WHERE id=?", (*columns.values(), ident))
            if phase:
                await c.execute("UPDATE jobs SET phase=? WHERE request_id=?", (phase, ident))

    async def notice(self, r, state, code=None, matches=None):
        async with self.db.tx() as c:
            old = await one(c, "SELECT notice_state FROM requests WHERE id=?", (r["id"],))
            if old["notice_state"] == state:
                return
            if state == "delivered":
                text = (f'{"ТЕСТ / mock: " if self.s.mode == "mock" else ""}Создано в CRM: {r["id"]}.\n'
                        f'Принято ботом {moscow(r["saved_at"])}; подтверждено в CRM {moscow(now())}')
                await enqueue(c, f'{r["id"]}:delivered', r["chat_id"], text, request_id=r["id"])
            else:
                text = f'Заявка {r["id"]}: {state}; код {code or "contact_match"}.'
                if matches:
                    text += " CRM ID: " + ", ".join(str(i) for i in matches)
                text += f'\nПодробности: /request {r["id"]}'
                for admin in await administrators(c, self.s, r['branch_key']):
                    await enqueue(c, f'alert:{r["id"]}:{state}:admin:{admin}', admin, text, request_id=r["id"])
                if state == "duplicate_review":
                    await enqueue(c, f'{r["id"]}:duplicate:user', r["chat_id"],
                                  "Контакт уже есть в CRM. Заявка передана администратору", request_id=r["id"])
            await c.execute("UPDATE requests SET notice_state=? WHERE id=?", (state, r["id"]))

    async def claim_contacts(self, r):
        async with self.db.tx() as c:
            for key in contact_keys(r["data"]):
                claim = await one(c, "SELECT request_id FROM contact_claims WHERE branch_key=? AND crm_branch_id=? AND contact_key=?",
                                  (r["branch_key"], r["crm_branch_id"], key))
                if claim and claim["request_id"] != r["id"]:
                    other = await one(c, "SELECT state FROM requests WHERE id=?", (claim["request_id"],))
                    if other["state"] != "delivered":
                        raise CRMError("contact_operation_unresolved")
            for key in contact_keys(r["data"]):
                await c.execute("INSERT OR REPLACE INTO contact_claims VALUES(?,?,?,?)", (r["branch_key"], r["crm_branch_id"], key, r["id"]))

    async def tick(self):
        async with self.lock:
            async with self.db.tx() as c:
                job = await one(c, "SELECT * FROM jobs WHERE state='pending' AND next_at<=? ORDER BY next_at, rowid LIMIT 1", (time.time(),))
                if not job:
                    return False
                await c.execute("UPDATE jobs SET state='processing',attempts=attempts+1,last_attempt_at=? WHERE request_id=?", (now(), job["request_id"]))
                r = await one(c, "SELECT * FROM requests WHERE id=?", (job["request_id"],))
                cursor = await c.execute("INSERT INTO delivery_attempts(request_id,branch_key,crm_branch_id,attempt,phase,started_at) VALUES(?,?,?,?,?,?)",
                                         (r["id"], r["branch_key"], r["crm_branch_id"], job["attempts"]+1, job["phase"], now()))
                attempt_id = cursor.lastrowid
            r["data"] = json.loads(r["data"])
            started = time.monotonic()
            try:
                if self.s.mode == "real":
                    self.s.for_branch(r['branch_key']).require_real_contract()
                await self.deliver(r, job["phase"])
                await self.db.execute("UPDATE jobs SET state='done',error=NULL WHERE request_id=?", (r["id"],))
                await self.db.execute("UPDATE delivery_attempts SET finished_at=?,outcome='completed' WHERE id=?", (now(), attempt_id))
                final = (await self.db.query("SELECT state FROM requests WHERE id=?", (r["id"],)))[0]["state"]
                log.info(dumps({"request_id": r["id"], "operation": "delivery", "status": final,
                                "attempts": job["attempts"]+1, "branch": r["branch_key"],
                                "duration_ms": int((time.monotonic()-started)*1000)}))
            except CRMError as exc:
                # Explicit non-ambiguous write rejection may safely re-enter check.
                if job["phase"] == "check" and not exc.ambiguous and exc.code in {"http_429", "crm_validation", "http_4xx", "http_401", "http_403"}:
                    await self.db.execute("UPDATE jobs SET phase='check' WHERE request_id=? AND phase='reconcile_create'", (r["id"],))
                attempts = job["attempts"] + 1
                retry = exc.retryable and attempts < self.s.max_attempts
                state = "retry_wait" if retry else "manual_review"
                delay = min(self.s.retry_cap, self.s.retry_base * 2 ** min(attempts, 16) * random.uniform(.8, 1.2))
                await self.change(r["id"], state, error=exc.code)
                await self.db.execute("UPDATE jobs SET state=?,next_at=?,error=? WHERE request_id=?", ("pending" if retry else "held", time.time() + delay, exc.code, r["id"]))
                await self.db.execute("UPDATE delivery_attempts SET finished_at=?,outcome=?,safe_error=? WHERE id=?", (now(), state, exc.code, attempt_id))
                await self.notice(r, state, exc.code)
                log.info(dumps({"request_id": r["id"], "operation": "delivery", "status": state,
                                "error": exc.code, "attempts": attempts, "branch": r["branch_key"],
                                "duration_ms": int((time.monotonic()-started)*1000)}))
            except (InputError, ValueError):
                await self.change(r["id"], "failed", error="configuration_or_payload")
                await self.db.execute("UPDATE jobs SET state='held',error='configuration_or_payload' WHERE request_id=?", (r["id"],))
                await self.db.execute("UPDATE delivery_attempts SET finished_at=?,outcome='failed',safe_error='configuration_or_payload' WHERE id=?", (now(), attempt_id))
                await self.notice(r, "failed", "configuration_or_payload")
            # DB errors deliberately escape: supervisor stops the service, does not claim success.
            return True

    async def deliver(self, r, phase):
        settings = self.s.for_branch(r['branch_key'])
        crm = self.crm.for_branch(r['branch_key'])
        if r['crm_branch_id'] != settings.branch['crm_id']:
            raise CRMError('branch_configuration_mismatch')
        snapshot = r["data"].get("crm_settings", {})
        if any(settings.branch.get(k) != v for k, v in snapshot.items()):
            raise CRMError("configuration_changed")
        payload = crm_payload(r, settings)
        await self.change(r["id"], "checking" if phase == "check" else "verifying")
        # One fresh, complete scan per attempt, reused only within this check.
        records = await crm.customers() if phase == "check" else None
        own = await crm.own(r["id"], records)
        if own:
            r["crm_id"] = own["id"]
            await self.change(r["id"], "crm_created", phase if phase in {"reconcile_comment", "verify"} else "comment_check", crm_id=own["id"])
        elif r["crm_id"]:
            # Local mapping exists: never create again if archived/deleted/invisible.
            raise CRMError("mapped_customer_missing", True)
        elif phase != "check":
            raise CRMError("write_outcome_unknown", True)
        else:
            await self.claim_contacts(r)
            duplicates = await crm.duplicates(r["data"], records)
            if duplicates:
                await self.change(r["id"], "duplicate_review", matches=dumps(duplicates), error="contact_match")
                await self.notice(r, "duplicate_review", matches=duplicates)
                return
            await self.change(r["id"], "creating", "reconcile_create")
            model = await crm.create(payload)
            r["crm_id"] = model["id"]
            await self.change(r["id"], "crm_created", "comment_check", crm_id=model["id"])
        # Verify before adding a communication, so automation conflicts don't cause more writes.
        await crm.verify(r["crm_id"], payload)
        if r["data"].get("comment"):
            mark = f'[promobot:{r["id"]}]'
            found = [x for x in await crm.comments(r["crm_id"]) if mark in x.get("comment", "").splitlines()]
            if len(found) > 1:
                raise CRMError("comment_not_unique")
            if not found:
                current = await self.db.query("SELECT phase FROM jobs WHERE request_id=?", (r["id"],))
                # own() recovery must not overwrite an uncertain comment phase.
                if r.get("communication_id") or phase in {"reconcile_comment", "verify"} or current[0]["phase"] == "reconcile_comment":
                    raise CRMError("comment_outcome_unknown", True)
                await self.change(r["id"], "comment_pending", "reconcile_comment")
                try:
                    created = await crm.add_comment(r["crm_id"], communication(r))
                except CRMError as exc:
                    if not exc.ambiguous:
                        await self.db.execute("UPDATE jobs SET phase='comment_check' WHERE request_id=?", (r["id"],))
                    raise
                await self.change(r["id"], "verifying", "verify", communication_id=created["id"])
                found = [x for x in await crm.comments(r["crm_id"]) if mark in x.get("comment", "").splitlines()]
            if len(found) != 1 or found[0].get("comment") != communication(r):
                raise CRMError("comment_verify_mismatch", True)
            expected_user = settings.branch.get("technical_user_id") or (990 if self.s.mode == "mock" else None)
            if found[0].get("user_id") != expected_user or found[0].get("type_id") != 1:
                raise CRMError("comment_author_mismatch")
            await self.change(r["id"], "verifying", "verify", communication_id=found[0]["id"])
        await crm.verify(r["crm_id"], payload)
        await self.change(r["id"], "delivered", "verify", verified_at=now(), error=None)
        await self.notice(r, "delivered")
