"""A sign-in protected A2A agent for trying kiro-a2a by hand (e.g. from Kiro).

    python tests/manual/run_secure_stack.py

Starts:
  - a test OpenID Connect provider on http://127.0.0.1:9200
    (client "desktop": browser sign-in that completes by itself;
     client "service" / secret "s3cret": client credentials)
  - the built-in demo agent behind `kiro-a2a serve` on http://127.0.0.1:9103,
    accepting only tokens from that provider (audience agent-api, scope agent.use),
    and asking the caller before any edit (--permissions ask)

Then:  kiro-a2a add secure http://127.0.0.1:9103 -g --client-id desktop
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # tests/, for fake_idp

from aiohttp import web  # noqa: E402
from fake_idp import FakeIdP  # noqa: E402

from kiro_a2a.acp import AcpAgent  # noqa: E402
from kiro_a2a.presets import preset  # noqa: E402
from kiro_a2a.server import Bridge, OAuthValidator, make_app  # noqa: E402


async def main() -> None:
    idp = await FakeIdP().start(9200)
    bridge = Bridge(
        AcpAgent(preset("demo")["command"], permissions="ask"),
        name="Secure Demo",
        description="Demo coding agent that requires OAuth sign-in and asks before editing.",
        public_url="http://127.0.0.1:9103/",
        oauth=OAuthValidator(idp.issuer, audience="agent-api", scopes=["agent.use"]),
        permission_timeout=300,
    )
    runner = web.AppRunner(make_app(bridge))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 9103).start()
    print(f"identity provider {idp.issuer}  |  secure agent http://127.0.0.1:9103/", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await idp.stop()


if __name__ == "__main__":
    asyncio.run(main())
