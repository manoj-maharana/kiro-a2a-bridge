"""Sign-in to A2A agents: OAuth flows, token cache and refresh, and serve's token checks."""

import asyncio
import json
import os

import aiohttp
import pytest
from fake_idp import FakeIdP
from helpers import running_bridge

from kiro_a2a.a2a_client import A2AClient
from kiro_a2a.auth import AuthConfig, TokenStore
from kiro_a2a.mcp import McpServer
from kiro_a2a.server import OAuthValidator


@pytest.fixture(autouse=True)
def token_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_A2A_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


async def protected_bridge(idp, **kwargs):
    validator = OAuthValidator(idp.issuer, audience="agent-api", scopes=["agent.use"])
    return running_bridge(oauth=validator, **kwargs)


async def call(client, message="hi", **kwargs):
    server = McpServer([client])
    try:
        tools = (await server.handle("tools/list", {}))["tools"]
        result = await server.handle("tools/call", {"name": tools[0]["name"], "arguments": {"message": message, **kwargs}})
    finally:
        await server.close()
    return result["content"][0]["text"], result["isError"]


def test_card_advertises_oauth_and_client_credentials_works(token_home):
    async def run():
        idp = await FakeIdP().start()
        try:
            async with await protected_bridge(idp) as (http, url, _):
                async with http.get(url + ".well-known/agent-card.json") as resp:
                    card = await resp.json()
                anonymous = await call(A2AClient(url, name="helper"))
                auth = AuthConfig(client_id="service", client_secret="s3cret")  # everything else from the card
                first = await call(A2AClient(url, name="helper", auth=auth))
                second = await call(A2AClient(url, name="helper", auth=auth))
                grants = list(idp.grants)
        finally:
            await idp.stop()
        return card, anonymous, first, second, grants

    card, anonymous, first, second, grants = asyncio.run(run())
    assert card["securitySchemes"]["oauth"]["type"] == "openIdConnect"
    assert card["security"] == [{"oauth": ["agent.use"]}]
    assert anonymous[1] is True  # no credentials -> refused
    assert "Echo: hi" in first[0] and first[1] is False
    assert "Echo: hi" in second[0]
    assert grants == ["client_credentials"]  # the second client reused the cached token
    saved = json.loads((token_home / "tokens.json").read_text())
    assert len(saved) == 1


def test_wrong_audience_is_rejected():
    async def run():
        idp = await FakeIdP().start()
        try:
            async with await protected_bridge(idp) as (_, url, _):
                auth = AuthConfig(client_id="service", client_secret="s3cret", audience="someone-else")
                return await call(A2AClient(url, name="helper", auth=auth)), list(idp.grants)
        finally:
            await idp.stop()

    (text, is_error), grants = asyncio.run(run())
    assert is_error and "HTTP 401" in text
    assert grants == ["client_credentials", "refresh_token"]  # retried once with a fresh token


def test_browser_sign_in_with_pkce_then_refresh():
    async def run():
        idp = await FakeIdP(expires_in=61).start()  # expires 1s after the 60s safety margin
        try:
            async with await protected_bridge(idp) as (_, url, _):
                auth = AuthConfig(type="oauth2", client_id="desktop", flow="authorization_code", issuer=idp.issuer)
                client = A2AClient(url, name="helper", auth=auth)
                prompts = []
                client.auth.prompt = prompts.append

                async with aiohttp.ClientSession() as browser:
                    async def fake_browser(link):
                        async with browser.get(link) as resp:  # follows the redirect to our callback
                            assert "Signed in" in await resp.text()

                    client.auth.open_browser = fake_browser
                    first = await call(client)
                await asyncio.sleep(1.2)
                second = await call(client)
                grants = list(idp.grants)
        finally:
            await idp.stop()
        return first, second, grants, prompts

    first, second, grants, prompts = asyncio.run(run())
    assert "Echo: hi" in first[0] and "Echo: hi" in second[0]
    assert grants == ["authorization_code", "refresh_token"]
    assert "Opening your browser" in prompts[0]


def test_device_sign_in_and_sign_in_required():
    async def run():
        idp = await FakeIdP().start()
        try:
            async with await protected_bridge(idp) as (_, url, _):
                auth = AuthConfig(type="oauth2", client_id="desktop", flow="device_code", issuer=idp.issuer)
                before = await call(A2AClient(url, name="helper", auth=auth))  # MCP may not start a device sign-in

                client = A2AClient(url, name="helper", auth=auth)
                prompts = []
                client.auth.prompt = prompts.append
                async with aiohttp.ClientSession() as http:
                    await client.auth.login(http, await client.fetch_card(http))
                after = await call(A2AClient(url, name="helper", auth=auth))  # uses the saved token
        finally:
            await idp.stop()
        return before, after, prompts

    before, after, prompts = asyncio.run(run())
    assert before[1] is True and "kiro-a2a login helper" in before[0]
    assert "WXYZ-1234" in prompts[0]
    assert "Echo: hi" in after[0] and after[1] is False


def test_static_bearer_and_api_key_still_work_alongside_oauth():
    async def run():
        idp = await FakeIdP().start()
        try:
            async with await protected_bridge(idp, api_key="k3y") as (_, url, _):
                bearer = await call(A2AClient(url, name="helper", auth=AuthConfig(type="bearer", token="k3y")))
                key = await call(A2AClient(url, name="helper", auth=AuthConfig(type="api_key", api_key="k3y")))
        finally:
            await idp.stop()
        return bearer, key

    bearer, key = asyncio.run(run())
    assert "Echo: hi" in bearer[0] and "Echo: hi" in key[0]


def test_config_entries_expand_env_and_validate(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "abc")
    client = A2AClient.from_config({
        "name": "x", "url": "https://agents.example.com/x",
        "auth": {"client_id": "id", "client_secret": "${MY_SECRET}", "scopes": "a b"},
    })
    assert client.auth.config.client_secret == "abc"
    assert client.auth.config.scopes == ["a", "b"]
    with pytest.raises(ValueError):
        AuthConfig.from_dict({"client_idd": "typo"})


def test_token_store_is_private(tmp_path):
    store = TokenStore(str(tmp_path / "t.json"))
    store.put("k", {"access_token": "x"})
    assert store.get("k") == {"access_token": "x"}
    if os.name != "nt":
        assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"
    store.put("k", None)
    assert store.get("k") is None
