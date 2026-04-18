import asyncio

from app.services import mt5_bridge


class FakeResponse:
    def __init__(self, status_code=200, json_payload=None, content_type='application/json'):
        self.status_code = status_code
        self._json_payload = json_payload or {}
        self.headers = {'content-type': content_type}

    def json(self):
        return self._json_payload


class FakeClient:
    def __init__(self, health_response=None, discovery_response=None, health_error=False, discovery_error=False, **kwargs):
        self.health_response = health_response or FakeResponse(200, {'ok': True})
        self.discovery_response = discovery_response or FakeResponse(200, {'accounts': []})
        self.health_error = health_error
        self.discovery_error = discovery_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, _url):
        if self.health_error:
            raise mt5_bridge.httpx.ConnectError('offline')
        return self.health_response

    async def post(self, _url, json=None):
        _ = json
        if self.discovery_error:
            raise mt5_bridge.httpx.ConnectError('not-ready')
        return self.discovery_response


def test_mt5_pairing_requires_bridge_url():
    result = asyncio.run(mt5_bridge.check_mt5_pairing_state(
        external_account_id='acct-1',
        bridge_url='',
        mt5_server='MetaQuotes',
    ))
    assert result['bridge_status'] == 'bridge_required'
    assert result['can_add_account'] is True


def test_mt5_pairing_detects_discovered_account(monkeypatch):
    discovered = {'accounts': [{'external_account_id': 'acct-1', 'display_label': 'My MT5'}]}

    def fake_async_client(*args, **kwargs):
        _ = args
        return FakeClient(discovery_response=FakeResponse(200, discovered), **kwargs)

    monkeypatch.setattr(mt5_bridge.httpx, 'AsyncClient', fake_async_client)
    result = asyncio.run(mt5_bridge.check_mt5_pairing_state(
        external_account_id='acct-1',
        bridge_url='https://bridge.local',
        mt5_server='MetaQuotes',
    ))

    assert result['bridge_status'] == 'bridge_reachable'
    assert result['discovery_status'] == 'discovered_account_ready'
    assert result['discovered_accounts'][0]['external_account_id'] == 'acct-1'
