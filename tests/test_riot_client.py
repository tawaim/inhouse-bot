"""RiotClient error handling: an outage must never look like "not found / unranked".

Uses httpx.MockTransport, so nothing here touches the network.
"""
import asyncio

import httpx
import pytest

from bot.services import riot_client
from bot.services.riot_client import RiotAuthError, RiotClient, RiotUnavailable


def _client(handler, monkeypatch) -> RiotClient:
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(riot_client.asyncio, "sleep", no_sleep)
    client = RiotClient(api_key="x")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _run(client: RiotClient, coro_fn):
    async def body():
        try:
            return await coro_fn()
        finally:
            await client.close()
    return asyncio.run(body())


def _always(status: int, json=None, headers=None):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, json=json, headers=headers)

    handler.calls = calls
    return handler


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
def test_server_errors_raise_unavailable_after_retries(monkeypatch, status):
    handler = _always(status)
    client = _client(handler, monkeypatch)
    with pytest.raises(RiotUnavailable):
        _run(client, lambda: client.get_solo_rank("p1"))
    assert len(handler.calls) == 3, "should retry before giving up"


def test_exhausted_rate_limit_raises_unavailable(monkeypatch):
    client = _client(_always(429, headers={"Retry-After": "1"}), monkeypatch)
    with pytest.raises(RiotUnavailable):
        _run(client, lambda: client.get_ranked_entries("p1"))


def test_timeouts_raise_unavailable(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = _client(handler, monkeypatch)
    with pytest.raises(RiotUnavailable):
        _run(client, lambda: client.get_account_by_riot_id("Name", "TAG"))


def test_unexpected_status_raises_unavailable_with_status(monkeypatch):
    client = _client(_always(400), monkeypatch)
    with pytest.raises(RiotUnavailable) as exc:
        _run(client, lambda: client.get_account_by_riot_id("Name", "TAG"))
    assert exc.value.status == 400


def test_auth_errors_still_raise_auth_error(monkeypatch):
    client = _client(_always(403), monkeypatch)
    with pytest.raises(RiotAuthError):
        _run(client, lambda: client.get_solo_rank("p1"))


def test_real_404_and_empty_list_mean_not_found(monkeypatch):
    client = _client(_always(404), monkeypatch)
    assert _run(client, lambda: client.get_account_by_riot_id("Name", "TAG")) is None
    client = _client(_always(200, json=[]), monkeypatch)
    assert _run(client, lambda: client.get_solo_rank("p1")) is None


def test_recovers_when_a_retry_succeeds(monkeypatch):
    responses = [
        httpx.Response(503),
        httpx.Response(200, json=[{"queueType": "RANKED_SOLO_5x5", "tier": "GOLD", "rank": "II",
                                   "leaguePoints": 40, "wins": 5, "losses": 3}]),
    ]
    client = _client(lambda request: responses.pop(0), monkeypatch)
    rank = _run(client, lambda: client.get_solo_rank("p1"))
    assert (rank.tier, rank.rank, rank.league_points) == ("GOLD", "II", 40)


def test_account_by_puuid(monkeypatch):
    handler = _always(200, json={"puuid": "p1", "gameName": "New Name", "tagLine": "NA1"})
    client = _client(handler, monkeypatch)
    account = _run(client, lambda: client.get_account_by_puuid("p1"))
    assert (account.puuid, account.game_name, account.tag_line) == ("p1", "New Name", "NA1")
    assert handler.calls[0].endswith("/riot/account/v1/accounts/by-puuid/p1")
    assert "americas.api.riotgames.com" in handler.calls[0]

    client = _client(_always(404), monkeypatch)
    assert _run(client, lambda: client.get_account_by_puuid("gone")) is None
