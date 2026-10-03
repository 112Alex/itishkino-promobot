import httpx
import pytest
from promobot.crm import AlfaCRM, CRMError


async def test_real_comment_requires_and_sends_configured_author(app):
    app.s.mode = 'real'
    app.s.crm_email = 'synthetic@example.invalid'
    app.s.crm_key = 'synthetic'
    app.s.branch['technical_user_id'] = 18
    bodies = []
    def handle(request):
        import json
        if request.url.path == '/v2api/auth/login':
            return httpx.Response(200, json={'token':'synthetic'})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'success':True,'model':{'id':1551}})
    crm = AlfaCRM(app.s, httpx.AsyncClient(base_url='https://example.invalid', transport=httpx.MockTransport(handle)))
    crm.limiter.interval = 0
    try:
        assert (await crm.add_comment(888,'Тест'))['id'] == 1551
        assert bodies == [{'type_id':1,'comment':'Тест','user_id':18}]
        for missing in (None,0,True):
            app.s.branch['technical_user_id'] = missing
            with pytest.raises(CRMError, match='comment_author_unconfigured'):
                await crm.add_comment(888,'Тест')
        assert len(bodies) == 1
    finally:
        await crm.close()
