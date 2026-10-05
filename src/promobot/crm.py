import asyncio
import json
import re
import time
import httpx
import phonenumbers
from .domain import InputError, username, contact_keys


class CRMError(Exception):
    def __init__(self, code, retryable=False, ambiguous=False):
        super().__init__(code)
        self.code, self.retryable, self.ambiguous = code, retryable, ambiguous


class Limiter:
    def __init__(self, interval=.25):
        self.interval, self.lock, self.next = interval, asyncio.Lock(), 0.

    async def wait(self):
        async with self.lock:
            await asyncio.sleep(max(0, self.next - time.monotonic()))
            self.next = time.monotonic() + self.interval


class AlfaCRM:
    def __init__(self, settings, client=None):
        self.s = settings
        self.client = client or httpx.AsyncClient(base_url=settings.crm_url, timeout=settings.timeout, trust_env=False)
        self.limiter = Limiter(settings.interval)
        self.token, self.expires, self.auth_lock = None, 0., asyncio.Lock()

    async def raw(self, path, body, write=False, token=None):
        await self.limiter.wait()
        try:
            response = await self.client.post(path, json=body, headers={"X-ALFACRM-TOKEN": token} if token else {})
        except (httpx.TimeoutException, httpx.TransportError):
            raise CRMError("network", True, write) from None
        status = response.status_code
        if status in {401, 403}:
            raise CRMError(f"http_{status}")
        if status == 429:
            raise CRMError("http_429", True)
        if status >= 500:
            raise CRMError("http_5xx", True, write)
        if status < 200 or status >= 300:
            raise CRMError("http_4xx")
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise CRMError("invalid_json", True, write) from None
        if data.get("errors") or data.get("success") is False:
            raise CRMError("crm_validation")
        if data.get("status", 200) >= 400:
            raise CRMError("crm_error", False, write)
        if write and (data.get("success") is not True or not isinstance(data.get("model"), dict) or not data["model"].get("id")):
            raise CRMError("write_result_missing", True, True)
        return data

    async def login(self):
        async with self.auth_lock:
            if self.token and self.expires > time.monotonic():
                return
            if self.s.mode == "real" and (not self.s.crm_email or not self.s.crm_key):
                raise CRMError("crm_not_configured")
            data = await self.raw("/v2api/auth/login", {"email": self.s.crm_email, "api_key": self.s.crm_key})
            if not isinstance(data.get("token"), str) or not data["token"]:
                raise CRMError("auth_invalid")
            self.token, self.expires = data["token"], time.monotonic() + 3300

    async def call(self, path, body, write=False):
        await self.login()
        try:
            return await self.raw(path, body, write, self.token)
        except CRMError as exc:
            if exc.code != "http_401":
                raise
            async with self.auth_lock:
                self.token, self.expires = None, 0
            await self.login()
            return await self.raw(path, body, write, self.token)

    def path(self, resource):
        return f'/v2api/{self.s.branch["crm_id"]}/{resource}'

    async def index(self, path, filters=None):
        result, total, seen = [], None, set()
        for page in range(1000):
            data = await self.call(path, {**(filters or {}), "page": page})
            items, n = data.get("items"), data.get("total")
            if (not isinstance(items, list) or not isinstance(n, int) or n < 0 or
                data.get("page") != page or data.get("count") != len(items) or
                (total is not None and n != total)):
                raise CRMError("incomplete_search", True)
            total = n
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), int) or item["id"] in seen:
                    raise CRMError("incomplete_search", True)
                seen.add(item["id"])
                result.append(item)
            if len(result) == total:
                return result
            if not items or len(result) > total:
                raise CRMError("incomplete_search", True)
        raise CRMError("search_limit")

    async def customers(self):
        # Fresh complete scan includes leads, clients and archives; no stale index.
        records = await self.index(self.path("customer/index"), {"is_study": 2, "removed": 1})
        for r in records:
            if self.s.branch["crm_id"] not in r.get("branch_ids", []):
                raise CRMError("branch_search_mismatch")
        return records

    async def own(self, request_id):
        records = await self.customers()
        matches = [r for r in records if r.get(self.s.branch["request_field"]) == request_id]
        if len(matches) > 1:
            raise CRMError("request_id_not_unique")
        return matches[0] if matches else None

    def contacts(self, record):
        result = set()
        for p in record.get("phone", []):
            if not isinstance(p, str):
                raise CRMError("contact_format_unverified")
            if p:
                try:
                    # Legacy CRM numbers may be unassigned according to numbering
                    # metadata. Keep their canonical keys instead of blocking all
                    # new requests; input validation remains stricter.
                    value = p.strip()
                    if not re.fullmatch(r"\+?[0-9\s().-]+", value):
                        raise ValueError
                    parsed = phonenumbers.parse(value, "RU" if not value.startswith("+") else None)
                    if not phonenumbers.is_possible_number(parsed) or parsed.extension:
                        raise ValueError
                    result.add("phone:" + phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164))
                except (ValueError, phonenumbers.NumberParseException):
                    # Cannot reliably exclude contact equality in unsupported formats.
                    raise CRMError("contact_format_unverified") from None
        for u in record.get("web", []):
            if not isinstance(u, str):
                raise CRMError("contact_format_unverified")
            if "t.me/" in u.lower() or u.startswith("@"):
                try:
                    result.add("telegram:" + username(u))
                except InputError:
                    raise CRMError("contact_format_unverified") from None
        return result

    async def duplicates(self, data):
        keys = contact_keys(data)
        return [r["id"] for r in await self.customers() if keys & self.contacts(r)]

    async def create(self, payload):
        return (await self.call(self.path("customer/create"), payload, True))["model"]

    async def comments(self, customer_id):
        return await self.index(self.path(f"communication/index?class=Customer&related_id={customer_id}"))

    async def add_comment(self, customer_id, comment):
        body = {"type_id": 1, "comment": comment}
        if self.s.mode == "real":
            author = self.s.branch.get("technical_user_id")
            if not isinstance(author, int) or isinstance(author, bool) or author <= 0:
                raise CRMError("comment_author_unconfigured")
            body["user_id"] = author
        return (await self.call(self.path(f"communication/create?class=Customer&related_id={customer_id}"),
                                body, True))["model"]

    async def verify(self, ident, payload):
        items = await self.index(self.path("customer/index"), {"id": ident, "is_study": 2, "removed": 1})
        if len(items) != 1 or items[0]["id"] != ident:
            raise CRMError("verify_missing", True)
        record = items[0]
        if self.s.branch.get("initial_unassigned") is True:
            if "lead_status_id" not in record or record["lead_status_id"] is not None:
                raise CRMError("verify_mismatch")
        for k, expected in payload.items():
            got = record.get(k)
            if k == "assigned_id" and got in (None, 0) and k in record:
                continue
            if got != expected:
                raise CRMError("verify_mismatch")
        return record

    async def close(self):
        await self.client.aclose()
