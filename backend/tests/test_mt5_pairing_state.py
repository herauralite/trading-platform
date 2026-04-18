import asyncio
import socket

from app.services import mt5_bridge


def test_mt5_pairing_requires_bridge_without_url():
    result = asyncio.run(mt5_bridge.check_mt5_pairing_state(
        external_account_id='',
        bridge_url='',
        mt5_server='',
    ))
    assert result['bridge_status'] == 'bridge_required'
    assert result['discovery_status'] == 'bridge_required'
    assert result['can_add_account'] is False


def test_mt5_pairing_is_safe_non_probing_even_with_user_bridge_url(monkeypatch):
    called = {'network': False}

    def fail_if_called(*args, **kwargs):
        called['network'] = True
        raise AssertionError('network should not be called during pairing check')

    monkeypatch.setattr(socket, 'create_connection', fail_if_called)

    result = asyncio.run(mt5_bridge.check_mt5_pairing_state(
        external_account_id='acct-1',
        bridge_url='https://untrusted-user-input.example',
        mt5_server='MetaQuotes',
    ))

    assert called['network'] is False
    assert result['implementation_mode'] == 'safe_non_probing_pairing'
    assert result['bridge_status'] == 'bridge_registration_pending'
    assert result['discovery_status'] == 'account_id_provided'
    assert result['can_add_account'] is True


def test_mt5_pairing_waits_for_trusted_worker_linkage():
    result = asyncio.run(mt5_bridge.check_mt5_pairing_state(
        external_account_id='acct-2',
        bridge_url='https://ignored-for-probing.local',
        mt5_server='MetaQuotes',
        bridge_id='bridge-123',
    ))

    assert result['bridge_status'] == 'waiting_for_bridge_worker'
    assert result['registration']['bridge_id_provided'] is True
    assert result['registration']['bridge_url_provided'] is True
