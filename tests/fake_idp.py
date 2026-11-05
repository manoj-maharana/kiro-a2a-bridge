"""A small OpenID Connect provider for the tests: client credentials,
authorization code + PKCE, device code, refresh tokens, and JWT access tokens."""

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import jwt
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import rsa


class FakeIdP:
    def __init__(self, expires_in: int = 3600):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.expires_in = expires_in
        self.issuer = ""
        self.codes: dict = {}
        self.devices: dict = {}
        self.grants: list[str] = []
        self.refresh_tokens: dict = {}
        self.clients = {"service": "s3cret", "desktop": None}

    def token(self, client_id: str, scope: str, audience: str) -> dict:
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": client_id, "aud": audience, "scope": scope,
                  "iat": now, "exp": now + self.expires_in}
        access = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})
        refresh = "r-" + secrets.token_hex(8)
        self.refresh_tokens[refresh] = (client_id, scope, audience)
        return {"access_token": access, "token_type": "Bearer", "expires_in": self.expires_in,
                "refresh_token": refresh, "scope": scope}

    async def discovery(self, request):
        return web.json_response({
            "issuer": self.issuer,
            "token_endpoint": self.issuer + "/token",
            "authorization_endpoint": self.issuer + "/authorize",
            "device_authorization_endpoint": self.issuer + "/device",
            "jwks_uri": self.issuer + "/jwks",
        })

    async def jwks(self, request):
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return web.json_response({"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]})

    async def authorize(self, request):
        q = request.query
        if q.get("client_id") not in self.clients or q.get("code_challenge_method") != "S256":
            raise web.HTTPBadRequest(text="bad request")
        code = secrets.token_hex(8)
        self.codes[code] = dict(q)
        raise web.HTTPFound(q["redirect_uri"] + "?" + urlencode({"code": code, "state": q["state"]}))

    async def device(self, request):
        form = await request.post()
        code = secrets.token_hex(8)
        self.devices[code] = {"form": dict(form), "polls": 0}
        return web.json_response({"device_code": code, "user_code": "WXYZ-1234",
                                  "verification_uri": self.issuer + "/activate", "interval": 0.05, "expires_in": 60})

    async def token_endpoint(self, request):
        form = dict(await request.post())
        grant = form.get("grant_type")
        self.grants.append(grant)
        client_id = form.get("client_id")
        error = lambda e: web.json_response({"error": e}, status=400)  # noqa: E731
        audience = form.get("audience", "agent-api")
        if grant == "client_credentials":
            if self.clients.get(client_id) != form.get("client_secret") or not form.get("client_secret"):
                return error("invalid_client")
            return web.json_response(self.token(client_id, form.get("scope", ""), audience))
        if grant == "authorization_code":
            saved = self.codes.pop(form.get("code"), None)
            if not saved or saved["redirect_uri"] != form.get("redirect_uri"):
                return error("invalid_grant")
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != saved["code_challenge"]:
                return error("invalid_grant")
            return web.json_response(self.token(client_id, saved.get("scope", ""), saved.get("audience", "agent-api")))
        if grant == "refresh_token":
            saved = self.refresh_tokens.pop(form.get("refresh_token"), None)
            if not saved:
                return error("invalid_grant")
            return web.json_response(self.token(*saved))
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            entry = self.devices.get(form.get("device_code"))
            if entry is None:
                return error("expired_token")
            entry["polls"] += 1
            if entry["polls"] < 2:
                return error("authorization_pending")
            return web.json_response(self.token(client_id, entry["form"].get("scope", ""), "agent-api"))
        return error("unsupported_grant_type")

    async def start(self, port: int = 0):
        app = web.Application()
        app.router.add_get("/.well-known/openid-configuration", self.discovery)
        app.router.add_get("/jwks", self.jwks)
        app.router.add_get("/authorize", self.authorize)
        app.router.add_post("/device", self.device)
        app.router.add_post("/token", self.token_endpoint)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", port).start()
        self.issuer = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        return self

    async def stop(self):
        await self.runner.cleanup()
