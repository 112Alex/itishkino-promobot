"""Persistent HTTP mock, using the same AlfaCRM adapter (no production endpoints)."""
import json
from urllib.parse import urlsplit, parse_qs
import httpx
from .storage import Store, dumps, rows


class MockCRM:
    def __init__(self, path, settings):
        self.store, self.s = Store(path), settings

    async def open(self):
        await self.store.open()
        await self.store.execute("CREATE TABLE IF NOT EXISTS mock_models(kind TEXT,id INTEGER,body TEXT,PRIMARY KEY(kind,id))")
        return self

    async def handle(self, req):
        url = urlsplit(str(req.url))
        body = json.loads(req.content)
        if url.path == "/v2api/auth/login":
            return httpx.Response(200, json={"token": "mock-only"})
        if req.headers.get("X-ALFACRM-TOKEN") != "mock-only":
            return httpx.Response(401)
        kind, operation = url.path.strip("/").split("/")[-2:]
        query = parse_qs(url.query)
        async with self.store.tx() as c:
            found = [json.loads(r["body"]) for r in await rows(c, "SELECT body FROM mock_models WHERE kind=? ORDER BY id", (kind,))]
            if operation == "create":
                ident = max([r["id"] for r in found] + [0]) + 1
                model = {**body, "id": ident}
                if kind == "communication":
                    model.update(related_id=int(query["related_id"][0]), branch_id=self.s.branch["crm_id"],
                                 user_id=self.s.branch.get("technical_user_id") or 990, **{"class": "Customer"})
                await c.execute("INSERT INTO mock_models VALUES(?,?,?)", (kind, ident, dumps(model)))
                return httpx.Response(200, json={"success": True, "errors": [], "model": model})
            if operation != "index":
                return httpx.Response(404)
            if kind == "branch":
                found = [{"id": self.s.branch["crm_id"], "name": self.s.branch["name"]}]
            elif kind == "lead-status":
                found = [{"id": self.s.branch["status_id"], "name": self.s.branch["status_name"], "pipeline_id": self.s.branch["pipeline_id"]}]
            elif kind == "lead-source":
                found = [{"id": self.s.branch["source_id"], "name": self.s.branch["source_name"]}]
            elif kind == "pipeline":
                found = [{"id": self.s.branch["pipeline_id"], "name": self.s.branch["pipeline_name"]}]
            if "id" in body:
                found = [r for r in found if r["id"] == body["id"]]
            if kind == "communication":
                found = [r for r in found if r["related_id"] == int(query["related_id"][0])]
            page = body.get("page", 0)
            items = found[page*50:(page+1)*50]
            return httpx.Response(200, json={"items": items, "total": len(found), "count": len(items), "page": page})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), base_url="https://mock.invalid")

    def application(self):
        from aiohttp import web
        async def handler(request):
            req = httpx.Request("POST", "https://mock.invalid" + request.path_qs,
                                headers=dict(request.headers), content=await request.read())
            response = await self.handle(req)
            return web.Response(body=response.content, status=response.status_code, content_type="application/json")
        app = web.Application()
        app.router.add_post("/{tail:.*}", handler)
        return app

    async def close(self):
        await self.store.close()
