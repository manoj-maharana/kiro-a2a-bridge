"""Authentication for calls to A2A agents.

Supported, per agent:
  none             no auth
  bearer           a fixed token
  api_key          a fixed key in a header
  oauth2           client_credentials (service to service),
                   authorization_code + PKCE (signs you in through the browser),
                   device_code (signs you in from a terminal)
  auto (default)   pick from the agent card's securitySchemes

OAuth endpoints come from, in order: the config, OpenID Connect discovery
(`issuer`, or the card's openIdConnectUrl), the card's oauth2 flows.
Tokens are cached on disk and refreshed when they expire.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import re
import secrets
import ssl
import sys
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse

import aiohttp
from aiohttp import web

from kiro_a2a.protocol import requirements_from_any, scheme_from_any

log = logging.getLogger(__name__)

AUTH_TYPES = ("auto", "none", "bearer", "api_key", "oauth2")
FLOWS = ("client_credentials", "authorization_code", "device_code")
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
EXPIRY_MARGIN = 60  # refresh this many seconds before a token expires


class AuthError(Exception):
    pass


class AuthRequired(AuthError):
    """A user sign-in is needed and cannot happen here (no terminal or browser)."""


def _expand(value):
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


@dataclass
class AuthConfig:
    type: str = "auto"
    token: str | None = None
    api_key: str | None = None
    api_key_header: str = "X-API-Key"
    flow: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] = field(default_factory=list)
    audience: str | None = None
    resource: str | None = None
    issuer: str | None = None
    token_url: str | None = None
    authorization_url: str | None = None
    device_authorization_url: str | None = None
    redirect_uri: str | None = None
    token_auth: str = "post"  # how the client secret is sent: post | basic

    @classmethod
    def from_dict(cls, data: dict | None) -> AuthConfig:
        data = _expand(dict(data or {}))
        if isinstance(data.get("scopes"), str):
            data["scopes"] = data["scopes"].split()
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown auth settings: {', '.join(sorted(unknown))}")
        config = cls(**data)
        if config.type not in AUTH_TYPES:
            raise ValueError(f"auth.type must be one of {AUTH_TYPES}")
        if config.flow and config.flow not in FLOWS:
            raise ValueError(f"auth.flow must be one of {FLOWS}")
        return config


@dataclass
class TlsConfig:
    ca_bundle: str | None = None
    client_cert: str | None = None
    client_key: str | None = None
    verify: bool = True

    @classmethod
    def from_dict(cls, data: dict | None) -> TlsConfig:
        return cls(**_expand(dict(data or {})))

    def ssl_context(self) -> ssl.SSLContext | bool | None:
        if not self.verify:
            return False
        if not (self.ca_bundle or self.client_cert):
            return None  # aiohttp default
        ctx = ssl.create_default_context(cafile=self.ca_bundle)
        if self.client_cert:
            ctx.load_cert_chain(self.client_cert, self.client_key)
        return ctx


def config_dir() -> str:
    if os.environ.get("KIRO_A2A_HOME"):
        return os.environ["KIRO_A2A_HOME"]
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "kiro-a2a")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "kiro-a2a")


class TokenStore:
    """Tokens in a JSON file that only the current user can read."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(config_dir(), "tokens.json")

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, ValueError):
            return {}

    def get(self, key: str) -> dict | None:
        return self._load().get(key)

    def put(self, key: str, value: dict | None) -> None:
        data = self._load()
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, self.path)


Prompt = Callable[[str], None]


def _stderr(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


class Authenticator:
    def __init__(
        self,
        config: AuthConfig,
        store: TokenStore | None = None,
        ssl_context=None,
        prompt: Prompt = _stderr,
        allow_browser: bool = True,
        allow_device: bool = False,
    ):
        self.config = config
        self.store = store or TokenStore()
        self.ssl = ssl_context
        self.prompt = prompt
        # Whether this process may start a sign-in that needs the user.
        self.allow_browser = allow_browser
        self.allow_device = allow_device
        self.open_browser: Callable = webbrowser.open
        self._resolved: dict | None = None
        self._access: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    # ---- what kind of auth ----------------------------------------------

    async def resolve(self, http: aiohttp.ClientSession, card: dict | None) -> dict:
        """Work out the auth method and, for OAuth, the endpoints and flow."""
        if self._resolved is not None:
            return self._resolved
        c = self.config
        kind = c.type
        card_kind, scheme, card_scopes = _from_card(card or {})
        if kind == "auto":
            if c.token:
                kind = "bearer"
            elif c.api_key:
                kind = "api_key"
            else:
                kind = card_kind
        if kind != card_kind:
            # set by hand to something the card doesn't describe: use only the config
            scheme, card_scopes = {}, []
        resolved: dict = {"type": kind}
        for field_name in ("client_secret", "token", "api_key", "client_id"):
            unset = re.findall(r"\$\{(\w+)\}", getattr(c, field_name) or "")
            if unset:
                raise AuthError(
                    f"the environment variable {unset[0]} (used for auth.{field_name}) is not set; "
                    "set it where Kiro runs and restart Kiro")
        if kind == "bearer" and not c.token:
            raise AuthError("this agent needs a bearer token: set auth.token (e.g. \"${MY_TOKEN}\")")
        if kind == "api_key":
            if not c.api_key:
                raise AuthError("this agent needs an API key: set auth.api_key (e.g. \"${MY_KEY}\")")
            resolved["header"] = scheme.get("name") if scheme.get("in") == "header" else c.api_key_header
        if kind == "oauth2":
            resolved.update(await self._oauth_settings(http, scheme, card_scopes))
        self._resolved = resolved
        return resolved

    async def _oauth_settings(self, http, scheme: dict, card_scopes: list[str]) -> dict:
        c = self.config
        if not c.client_id:
            raise AuthError("this agent uses OAuth: set auth.client_id")
        endpoints = {"token_url": None, "authorization_url": None, "device_authorization_url": None}
        flows = scheme.get("flows") or {}
        for flow_name, flow in flows.items():
            endpoints["token_url"] = endpoints["token_url"] or flow.get("tokenUrl")
            if flow_name == "authorizationCode":
                endpoints["authorization_url"] = flow.get("authorizationUrl")
            if flow_name == "deviceCode":
                endpoints["device_authorization_url"] = flow.get("deviceAuthorizationUrl")
        discovery = c.issuer or scheme.get("openIdConnectUrl")
        if discovery:
            meta = await self._discover(http, discovery)
            endpoints["token_url"] = meta.get("token_endpoint") or endpoints["token_url"]
            endpoints["authorization_url"] = meta.get("authorization_endpoint") or endpoints["authorization_url"]
            endpoints["device_authorization_url"] = (
                meta.get("device_authorization_endpoint") or endpoints["device_authorization_url"]
            )
        for key in endpoints:
            endpoints[key] = getattr(c, key) or endpoints[key]
        if not endpoints["token_url"]:
            raise AuthError("could not find the OAuth token URL: set auth.issuer or auth.token_url")

        flow = c.flow
        if not flow:
            if c.client_secret and ("clientCredentials" in flows or not flows):
                flow = "client_credentials"
            elif endpoints["authorization_url"]:
                flow = "authorization_code"
            elif endpoints["device_authorization_url"]:
                flow = "device_code"
            else:
                flow = "client_credentials"
        if flow == "client_credentials" and not c.client_secret:
            raise AuthError("client_credentials needs auth.client_secret (e.g. \"${MY_CLIENT_SECRET}\")")
        if flow == "authorization_code" and not endpoints["authorization_url"]:
            raise AuthError("authorization_code needs auth.authorization_url or auth.issuer")
        if flow == "device_code" and not endpoints["device_authorization_url"]:
            raise AuthError("device_code needs auth.device_authorization_url or auth.issuer")
        scopes = c.scopes or card_scopes
        return {**endpoints, "flow": flow, "scopes": scopes}

    async def _discover(self, http, url: str) -> dict:
        if not url.endswith("/.well-known/openid-configuration"):
            url = url.rstrip("/") + "/.well-known/openid-configuration"
        async with http.get(url, ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise AuthError(f"OpenID discovery failed at {url}: HTTP {resp.status}")
            return await resp.json(content_type=None)

    # ---- headers ----------------------------------------------------------

    async def headers(self, http: aiohttp.ClientSession, card: dict | None) -> dict:
        resolved = await self.resolve(http, card)
        kind = resolved["type"]
        if kind == "bearer":
            return {"Authorization": f"Bearer {self.config.token}"}
        if kind == "api_key":
            return {resolved["header"]: self.config.api_key}
        if kind == "oauth2":
            return {"Authorization": f"Bearer {await self.access_token(http)}"}
        return {}

    def invalidate(self) -> None:
        """Forget the current access token (e.g. after the agent said 401)."""
        self._access = None
        if self._resolved and self._resolved["type"] == "oauth2":
            saved = self.store.get(self._key()) or {}
            if saved:
                saved["expires_at"] = 0
                self.store.put(self._key(), saved)

    def logout(self) -> None:
        self._access = None
        if self._resolved and self._resolved["type"] == "oauth2":
            self.store.put(self._key(), None)

    def _key(self) -> str:
        r, c = self._resolved, self.config
        return "|".join([r["token_url"], c.client_id or "", " ".join(sorted(r["scopes"])), c.audience or c.resource or ""])

    # ---- OAuth tokens -----------------------------------------------------

    async def access_token(self, http: aiohttp.ClientSession, interactive: bool | None = None) -> str:
        async with self._lock:
            now = time.time()
            if self._access and self._access[1] > now:
                return self._access[0]
            saved = self.store.get(self._key()) or {}
            if saved.get("access_token") and saved.get("expires_at", 0) > now:
                return self._remember(saved)
            if saved.get("refresh_token"):
                try:
                    return self._remember(await self._refresh(http, saved["refresh_token"]))
                except AuthError as exc:
                    log.debug("token refresh failed, signing in again: %s", exc)
            return self._remember(await self._new_token(http, interactive))

    async def login(self, http: aiohttp.ClientSession, card: dict | None) -> None:
        """Sign in now, from a terminal (used by `kiro-a2a login`)."""
        resolved = await self.resolve(http, card)
        if resolved["type"] != "oauth2":
            self.prompt(f"Nothing to sign in to: this agent uses '{resolved['type']}' auth.")
            return
        self.logout()
        self.allow_device = True
        await self.access_token(http, interactive=True)

    def _remember(self, saved: dict) -> str:
        self._access = (saved["access_token"], saved.get("expires_at", time.time() + 300))
        return saved["access_token"]

    async def _new_token(self, http, interactive: bool | None) -> dict:
        flow = self._resolved["flow"]
        if flow == "client_credentials":
            return await self._token_request(http, {"grant_type": "client_credentials"})
        if flow == "authorization_code":
            if not (self.allow_browser if interactive is None else interactive):
                raise AuthRequired("sign-in needed: run `kiro-a2a login <agent>`")
            return await self._browser_login(http)
        if not (self.allow_device if interactive is None else interactive):
            raise AuthRequired("sign-in needed: run `kiro-a2a login <agent>` in a terminal")
        return await self._device_login(http)

    async def _refresh(self, http, refresh_token: str) -> dict:
        token = await self._token_request(http, {"grant_type": "refresh_token", "refresh_token": refresh_token})
        token.setdefault("refresh_token", refresh_token)
        self.store.put(self._key(), token)
        return token

    async def _token_request(self, http, form: dict, save: bool = True) -> dict:
        c, r = self.config, self._resolved
        form = dict(form)
        if r["scopes"] and form["grant_type"] in ("client_credentials", "refresh_token", DEVICE_GRANT):
            form.setdefault("scope", " ".join(r["scopes"]))
        for extra in ("audience", "resource"):
            if getattr(c, extra) and form["grant_type"] != "refresh_token":
                form[extra] = getattr(c, extra)
        auth = None
        form["client_id"] = c.client_id
        if c.client_secret:
            if c.token_auth == "basic":
                auth = aiohttp.BasicAuth(c.client_id, c.client_secret)
                form.pop("client_id")
            else:
                form["client_secret"] = c.client_secret
        async with http.post(r["token_url"], data=form, auth=auth, ssl=self.ssl,
                             timeout=aiohttp.ClientTimeout(total=30)) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                raise AuthError(f"token endpoint returned HTTP {resp.status} without JSON") from None
        if not isinstance(body, dict) or "access_token" not in body:
            raise AuthError(_oauth_error(body))
        body["expires_at"] = time.time() + float(body.get("expires_in", 3600)) - EXPIRY_MARGIN
        if save:
            self.store.put(self._key(), body)
        return body

    async def _browser_login(self, http) -> dict:
        c, r = self.config, self._resolved
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        loop = asyncio.get_running_loop()
        result: asyncio.Future = loop.create_future()

        async def callback(request: web.Request) -> web.Response:
            query = request.query
            if query.get("state") != state:
                return web.Response(status=400, text="State mismatch. Please try signing in again.")
            if not result.done():
                result.set_result(dict(query))
            ok = "code" in query
            return web.Response(
                content_type="text/html",
                text="<h3>Signed in. You can close this tab.</h3>" if ok else "<h3>Sign-in failed.</h3>",
            )

        wanted = urlparse(c.redirect_uri) if c.redirect_uri else None
        app = web.Application()
        app.router.add_get(wanted.path if wanted and wanted.path else "/callback", callback)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", wanted.port if wanted and wanted.port else 0).start()
            port = runner.addresses[0][1]
            redirect_uri = c.redirect_uri or f"http://127.0.0.1:{port}/callback"
            params = {
                "response_type": "code",
                "client_id": c.client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
            if r["scopes"]:
                params["scope"] = " ".join(r["scopes"])
            for extra in ("audience", "resource"):
                if getattr(c, extra):
                    params[extra] = getattr(c, extra)
            sep = "&" if "?" in r["authorization_url"] else "?"
            url = r["authorization_url"] + sep + urlencode(params)
            self.prompt(f"Opening your browser to sign in. If it does not open, visit:\n{url}")
            opened = self.open_browser(url)
            if inspect.isawaitable(opened):
                await opened
            try:
                answer = await asyncio.wait_for(result, timeout=300)
            except asyncio.TimeoutError:
                raise AuthError("sign-in timed out after 5 minutes") from None
        finally:
            await runner.cleanup()
        if "code" not in answer:
            raise AuthError(_oauth_error(answer))
        return await self._token_request(http, {
            "grant_type": "authorization_code",
            "code": answer["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        })

    async def _device_login(self, http) -> dict:
        c, r = self.config, self._resolved
        form = {"client_id": c.client_id}
        if r["scopes"]:
            form["scope"] = " ".join(r["scopes"])
        if c.audience:
            form["audience"] = c.audience
        async with http.post(r["device_authorization_url"], data=form, ssl=self.ssl,
                             timeout=aiohttp.ClientTimeout(total=30)) as resp:
            start = await resp.json(content_type=None)
        if "device_code" not in start:
            raise AuthError(_oauth_error(start))
        where = start.get("verification_uri_complete") or start.get("verification_uri") or start.get("verification_url")
        self.prompt(f"To sign in, open {where} and enter the code: {start.get('user_code')}")
        interval = float(start.get("interval", 5))
        deadline = time.time() + float(start.get("expires_in", 900))
        while time.time() < deadline:
            await asyncio.sleep(interval)
            try:
                return await self._token_request(http, {"grant_type": DEVICE_GRANT, "device_code": start["device_code"]})
            except AuthError as exc:
                if "authorization_pending" in str(exc):
                    continue
                if "slow_down" in str(exc):
                    interval += 5
                    continue
                raise
        raise AuthError("the sign-in code expired; run the login again")


def _from_card(card: dict) -> tuple[str, dict, list[str]]:
    """Pick the first usable security requirement from an agent card."""
    schemes = {name: scheme_from_any(s) for name, s in (card.get("securitySchemes") or {}).items()}
    requirements = requirements_from_any(card) or [{name: []} for name in schemes]
    order = {"oauth2": 0, "openIdConnect": 0, "http": 1, "apiKey": 2}
    best = None
    for requirement in requirements:
        for name, scopes in requirement.items():
            scheme = schemes.get(name) or {}
            kind = scheme.get("type")
            if kind not in order or (kind == "http" and str(scheme.get("scheme", "")).lower() != "bearer"):
                continue
            if best is None or order[kind] < order[best[1].get("type")]:
                best = (name, scheme, list(scopes or []))
    if best is None:
        return "none", {}, []
    _, scheme, scopes = best
    kind = {"oauth2": "oauth2", "openIdConnect": "oauth2", "http": "bearer", "apiKey": "api_key"}[scheme["type"]]
    return kind, scheme, scopes


def _oauth_error(body: dict) -> str:
    error = body.get("error") or "unknown_error"
    detail = body.get("error_description") or body.get("message") or ""
    return f"OAuth error: {error} {detail}".strip()
